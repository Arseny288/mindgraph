"""cli.py — argparse, конфиг и оркестрация (фаза 1: JSON-контракт).

Два режима:

* legacy human:  ``mindgraph <path> [--serve|--stats|--no-llm|--regroup|--force]``
* машинный:      подкоманды ``scan/graph/node/impact/boundary/tests/verify``.

Машинный контракт (фаза 1):
  * результат — только в stdout, только валидный JSON;
  * все логи/предупреждения — в stderr;
  * каждый JSON-ответ содержит ``"schema_version"``;
  * коды возврата: 0 успех, 1 ошибка выполнения, 2 неверные аргументы,
    3 нарушение границы (verify).

Приоритет настроек: CLI > MINDGRAPH_* env > mindgraph.toml > ~/.config/... >
дефолты (см. mindgraph/config.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import emit, llmcache, model, verify as verify_mod, testselect, impact as impact_mod
from . import __version__
from .config import resolve_config
from .group import PROVIDERS, DEEPSEEK_MODEL, ANTHROPIC_MODEL

log = logging.getLogger("mindgraph")

# Корень ИНСТРУМЕНТА mindgraph (не анализируемой репы) — ключи провайдеров это
# креды самого инструмента, .env лежит рядом с pyproject.toml/mindgraph/.
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROVIDER_MODELS = {"deepseek": DEEPSEEK_MODEL, "anthropic": ANTHROPIC_MODEL}

# Коды возврата (фаза 1).
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_BOUNDARY = 3

SCHEMA_VERSION = "1"


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


def _out_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _load_graph(dir_: str) -> dict:
    try:
        return model.load_graph(dir_)
    except (OSError, json.JSONDecodeError) as e:
        print(f"mindgraph: не удалось прочитать graph.json для {dir_!r} ({e})",
              file=sys.stderr)
        print(f"  сначала выполните: mindgraph scan {dir_}", file=sys.stderr)
        sys.exit(EXIT_RUNTIME)


def _resolve_config(root: str, args) -> "object":
    config = resolve_config(root, explicit_path=getattr(args, "config", None))
    if getattr(args, "provider", None) is not None:
        config.provider = args.provider
    if getattr(args, "model", None) is not None:
        config.model = args.model
    if getattr(args, "port", None) is not None:
        config.port = args.port
    if getattr(args, "no_llm", False):
        config.no_llm = True
    for extra in (getattr(args, "ignore", None) or []):
        config.ignore = list(dict.fromkeys(config.ignore + [extra]))
    return config


# --- Подкоманды ------------------------------------------------------------

def _cmd_scan(args) -> int:
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"mindgraph: {args.dir!r} не каталог", file=sys.stderr)
        return EXIT_RUNTIME

    config = _resolve_config(root, args)
    cache_base = llmcache.resolve_base(config.cache_dir)

    graph, from_cache = emit.load_or_build(
        root, provider_name=config.provider, no_llm=config.no_llm,
        force=args.force, regroup=args.regroup, extra_ignore=config.ignore,
        model=config.model, cache_base=cache_base,
        entry_points=config.entry_points,
        node_min=config.node_min, node_max=config.node_max)

    path = emit.graph_path(root)
    if from_cache:
        print(f"graph.json актуален ({path}), переиспользую — --force для пересчёта",
              file=sys.stderr)
    else:
        print(f"записано: {path}", file=sys.stderr)

    if args.json:
        _out_json({
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "graph_path": path,
            "from_cache": from_cache,
            "stats": graph["stats"],
            "warnings": graph["warnings"],
        })
        return EXIT_OK

    if args.stats:
        _print_stats(graph)
    if args.serve:
        from . import serve
        serve.run(root, port=config.port, open_browser=config.open_browser)
    return EXIT_OK


def _cmd_graph(args) -> int:
    graph = _load_graph(args.dir)
    if args.json:
        _out_json({"schema_version": SCHEMA_VERSION, **graph})
        return EXIT_OK
    print(f"репо {graph['repo']['name']}: {len(graph['nodes'])} узлов, "
          f"{graph['stats']['symbols']} символов")
    for n in graph["nodes"]:
        print(f"  {n['id']}\t{n['name']}  ({len(n['symbols'])} символов)")
    return EXIT_OK


def _cmd_node(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.id)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME
    view = model.node_view(graph, node)

    if args.json:
        _out_json({
            "schema_version": SCHEMA_VERSION,
            "node": {
                "id": node["id"], "name": node["name"],
                "summary": node.get("summary", ""),
                "files": node.get("files", []),
                "symbols": node.get("symbols", []),
            },
            "owned": {"files": view["owned_files"],
                      "symbols": view["owned_symbols"]},
            "contracts": view["contracts"],
            "foreign": {"files": view["foreign_files"]},
            "mixed": view["mixed"],
        })
        return EXIT_OK

    print(f"узел {node['name']} ({node['id']})")
    print(f"  файлов: {len(view['owned_files'])}, символов: {len(view['owned_symbols'])}, "
          f"контрактов: {len(view['contracts'])}")
    return EXIT_OK


def _cmd_impact(args) -> int:
    graph = _load_graph(args.dir)
    result = impact_mod.impact(graph, args.query, depth=args.depth)
    result["schema_version"] = SCHEMA_VERSION
    if args.json:
        _out_json(result)
        return EXIT_OK
    print(f"impact {args.query}: {len(result['symbols'])} символов, "
          f"{len(result['files'])} файлов")
    for f in result["files"]:
        print(f"  {f}")
    return EXIT_OK


def _cmd_boundary(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.node)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME
    bnd = model.boundary(graph, node)
    if args.json:
        _out_json(bnd)
        return EXIT_OK
    print(f"границы узла {bnd['node_name']}:")
    print(f"  allow_write: {bnd['allow_write']}")
    print(f"  deny_write:  {bnd['deny_write']}")
    print(f"  контрактов:  {len(bnd['contracts'])}")
    return EXIT_OK


def _cmd_tests(args) -> int:
    graph = _load_graph(args.dir)
    if args.for_node:
        try:
            node = model.node_by_ref(graph, args.for_node)
        except KeyError as e:
            print(f"mindgraph: {e}", file=sys.stderr)
            return EXIT_RUNTIME
        result = testselect.node_tests(graph, node, f"for-node {args.for_node}")
    else:
        diff_text = _read_diff(args.for_diff)
        changed = {f["path"] for f in verify_mod._parse_diff(diff_text)}
        result = testselect.diff_tests(graph, changed, f"for-diff {args.for_diff}")

    if args.json:
        _out_json(result)
        return EXIT_OK
    print(f"тесты ({result['query']}): {result['status']} — {len(result['tests'])}")
    for t in result["tests"]:
        print(f"  {t}")
    if result["status"] != model.STATUS_OK:
        print(f"  (unverified: {result.get('reason', '?')})", file=sys.stderr)
    return EXIT_OK


def _cmd_verify(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.node)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME

    diff_text = _read_diff(args.diff)
    result = verify_mod.verify(graph, node, diff_text,
                               original_dir=os.path.abspath(args.dir))
    _out_json(result)
    return EXIT_BOUNDARY if not result["ok"] else EXIT_OK


def _read_diff(diff: str) -> str:
    if diff == "-":
        return sys.stdin.read()
    try:
        with open(diff, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        print(f"mindgraph: не удалось прочитать дифф {diff!r} ({e})",
              file=sys.stderr)
        sys.exit(EXIT_RUNTIME)


# --- Парсер ----------------------------------------------------------------

def _add_llm_flags(p, with_regroup=True):
    p.add_argument("--provider", default=None, choices=sorted(PROVIDERS))
    p.add_argument("--model", default=None)
    p.add_argument("--ignore", action="append", default=[], metavar="DIR")
    p.add_argument("--config", default=None, metavar="FILE")
    p.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mindgraph",
        description="Смысловой оверлей поверх Python-репозитория.")
    ap.add_argument("--verbose", action="store_true",
                    help="лог этапов и прогресс в stderr (stdout остаётся чистым)")
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--version", action="version", version=f"mindgraph {__version__}")

    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("scan", help="построить/обновить graph.json")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-llm", action="store_true")
    g.add_argument("--regroup", action="store_true")
    _add_llm_flags(p)
    p.add_argument("--port", type=int, default=None)

    p = sub.add_parser("graph", help="весь граф (узлы, рёбра, метаданные)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("node", help="один узел: файлы, символы, зона, контракты")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("impact", help="транзитивное множество затронутого")
    p.add_argument("query", help="symbol_id или путь файла")
    p.add_argument("--depth", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("boundary", help="границы зоны узла")
    p.add_argument("node")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("tests", help="какие тесты прогонять")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--for-node", dest="for_node")
    g.add_argument("--for-diff", dest="for_diff")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("verify", help="проверить дифф против границы узла")
    p.add_argument("--node", required=True)
    p.add_argument("--diff", required=True, help="путь к диффу или '-' (stdin)")
    p.add_argument("--dir", default=".")

    return ap


def _list_providers() -> None:
    for name in sorted(PROVIDERS):
        print(f"{name:<10} модель по умолчанию: {_PROVIDER_MODELS[name]}")


_DISPATCH = {
    "scan": _cmd_scan,
    "graph": _cmd_graph,
    "node": _cmd_node,
    "impact": _cmd_impact,
    "boundary": _cmd_boundary,
    "tests": _cmd_tests,
    "verify": _cmd_verify,
}


def main(argv=None) -> int:
    _load_dotenv(os.path.join(_TOOL_ROOT, ".env"))
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="mindgraph: %(message)s", stream=sys.stderr)

    if getattr(args, "list_providers", False):
        _list_providers()
        return EXIT_OK

    if args.command is None:
        print("mindgraph: укажите подкоманду (см. --help)", file=sys.stderr)
        return EXIT_USAGE

    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
