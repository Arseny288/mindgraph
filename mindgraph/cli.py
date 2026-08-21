"""cli.py — argparse, конфиг и оркестрация этапов.

    mindgraph <path>            анализ + запись .mindgraph/graph.json
    mindgraph <path> --serve    то же + вьюер на localhost
    mindgraph <path> --no-llm   только статический анализ (не трогает LLM-разметку)
    mindgraph <path> --regroup  полный пересчёт LLM-группировки (кэшируется)
    mindgraph <path> --stats    напечатать метрики (§6) и выйти

Приоритет настроек: CLI > MINDGRAPH_* env > mindgraph.toml > ~/.config/... >
дефолты (см. mindgraph/config.py).
"""

from __future__ import annotations

import argparse
import os
import sys

from . import emit, llmcache
from . import __version__
from .config import resolve_config
from .group import PROVIDERS, DEEPSEEK_MODEL, ANTHROPIC_MODEL

# Корень ИНСТРУМЕНТА mindgraph (не анализируемой репы) — ключи провайдеров это
# креды самого инструмента, .env лежит рядом с pyproject.toml/mindgraph/.
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROVIDER_MODELS = {"deepseek": DEEPSEEK_MODEL, "anthropic": ANTHROPIC_MODEL}


def _load_dotenv(path: str) -> None:
    """Минимальный загрузчик .env (KEY=VALUE построчно), без внешних зависимостей.

    Не перезаписывает уже выставленные переменные окружения (шелл побеждает).
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _print_stats(graph: dict) -> None:
    repo, stats = graph["repo"], graph["stats"]
    total = stats["symbols"]

    print(f"файлов {repo['files']} · символов {total} · строк {repo['loc']}")

    unassigned_pct = (stats["unassigned"] / total * 100) if total else 0.0
    print(f"узлов {len(graph['nodes'])} · не назначено {stats['unassigned']} "
          f"({unassigned_pct:.1f}%)")

    ratio_pct = stats["shared_ratio"] * 100
    print(f"shared-символов {stats['shared_symbols']} ({ratio_pct:.1f}%)   "
          f"<- ключевая метрика")

    top = sorted((s for s in graph["symbols"] if s["shared"]),
                 key=lambda s: -len(s["owners"]))[:3]
    if top:
        parts = ", ".join(f"{s['id']} ({len(s['owners'])} узлов)" for s in top)
        print(f"топ shared: {parts}")

    if ratio_pct <= 15:
        hint = "узлы изолируемы, границы записи осмысленны"
    elif ratio_pct > 30:
        hint = "узлы протекают друг в друга, определение узла нужно менять"
    else:
        hint = "пограничная зона — присмотреться перед тем как резать границы"
    print(f"  ({hint})")

    if graph["warnings"]:
        print(f"\nwarnings ({len(graph['warnings'])}):", file=sys.stderr)
        for w in graph["warnings"][:10]:
            print(f"  ! {w}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mindgraph",
        description="Смысловой оверлей поверх Python-репозитория.")
    ap.add_argument("path", nargs="?", default=".", help="корень репозитория (по умолчанию .)")
    ap.add_argument("--serve", action="store_true", help="поднять вьюер и открыть браузер")
    ap.add_argument("--stats", action="store_true", help="напечатать метрики и выйти")
    ap.add_argument("--force", action="store_true", help="пересчитать даже если graph.json актуален")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--no-llm", action="store_true",
                       help="только статический анализ (LLM-разметку не перезаписывает)")
    group.add_argument("--regroup", action="store_true",
                       help="полный пересчёт LLM-группировки (перезапись узлов)")
    ap.add_argument("--provider", default=None, choices=sorted(PROVIDERS),
                    help="LLM-провайдер (по умолчанию из конфига: deepseek)")
    ap.add_argument("--model", default=None,
                    help="модель LLM (по умолчанию — дефолтная у провайдера)")
    ap.add_argument("--ignore", action="append", default=[], metavar="DIR",
                    help="не сканировать каталог с этим именем (можно несколько раз)")
    ap.add_argument("--config", default=None, metavar="FILE",
                    help="конфиг-файл вместо mindgraph.toml/пользовательского")
    ap.add_argument("--port", type=int, default=None, help="порт вьюера (--serve)")
    ap.add_argument("--list-providers", action="store_true",
                    help="показать доступных LLM-провайдеров и выйти")
    ap.add_argument("--version", action="version", version=f"mindgraph {__version__}")
    return ap


def _list_providers() -> None:
    for name in sorted(PROVIDERS):
        print(f"{name:<10} модель по умолчанию: {_PROVIDER_MODELS[name]}")


def main(argv=None) -> int:
    _load_dotenv(os.path.join(_TOOL_ROOT, ".env"))
    args = build_parser().parse_args(argv)

    if args.list_providers:
        _list_providers()
        return 0

    if not os.path.isdir(args.path):
        print(f"mindgraph: {args.path!r} не каталог", file=sys.stderr)
        return 1

    root = os.path.abspath(args.path)

    # CLI > env > конфиг-файлы > дефолты.
    config = resolve_config(root, explicit_path=args.config)
    if args.provider is not None:
        config.provider = args.provider
    if args.model is not None:
        config.model = args.model
    if args.port is not None:
        config.port = args.port
    if args.no_llm:
        config.no_llm = True
    if args.ignore:
        config.ignore = list(dict.fromkeys(config.ignore + args.ignore))

    cache_base = llmcache.resolve_base(config.cache_dir)

    graph, from_cache = emit.load_or_build(
        root, provider_name=config.provider, no_llm=config.no_llm,
        force=args.force, regroup=args.regroup, extra_ignore=config.ignore,
        model=config.model, cache_base=cache_base,
        entry_points=config.entry_points)

    path = emit.graph_path(root)
    if from_cache:
        print(f"graph.json актуален ({path}), переиспользую — --force для пересчёта",
              file=sys.stderr)
    else:
        print(f"записано: {path}", file=sys.stderr)

    if args.stats:
        _print_stats(graph)
        return 0

    if args.serve:
        from . import serve
        serve.run(root, port=config.port, open_browser=config.open_browser)

    return 0


if __name__ == "__main__":
    sys.exit(main())
