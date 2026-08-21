"""Сборка и запись graph.json (§5 спеки) — единственный контракт с вьюером.

Вьюер не знает про Python и ничего не вычисляет сам: shared/owners по
символам, агрегированные рёбра между узлами с весами и `weak`, все статы —
всё уже посчитано здесь. extract.py/group.py/shared.py остаются read-only
анализаторами; emit.py их оркестрирует и решает, что писать на диск и куда.

Кэш: если ``<root>/.mindgraph/graph.json`` уже есть и его ``repo.repo_hash``
совпадает с текущим — переиспользуем без пересчёта (если не ``--force``).
``repo_hash`` — sha256 по (путь, размер, mtime) просканированных .py файлов,
не по содержимому: дёшево, и как признак "не менялось" достаточно.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from .domains import assign_domains, UNASSIGNED_DOMAIN
from .extract import extract_repo, _iter_python_files, _iter_ignore_files, _rel
from .group import group_repo, make_provider, node_map, UNASSIGNED_NODE
from .shared import node_of, compute_owners_by_node, ENTRY_POINT_PATTERNS, _is_entry_point

GRAPH_VERSION = 2  # +domains, +domain_edges, +node.parent, +grouping (аддитивно, §5 спеки)


def graph_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), ".mindgraph", "graph.json")


def compute_repo_hash(root: str, extra_ignore=()) -> str:
    """sha256((relpath, size, mtime) по .py файлам + содержимое игнор-файлов).

    Игнор-файлы (.gitignore/.mindgraphignore) включены содержимым: правка
    правил исключения инвалидирует кэш наравне с правкой кода.
    """
    h = hashlib.sha256()
    for path in _iter_python_files(root, extra_ignore):
        rel = _rel(root, path)
        st = os.stat(path)
        h.update(f"{rel}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    for path, rel in _iter_ignore_files(root, extra_ignore):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            content = ""
        h.update(f"ignore:{rel}:{content}\n".encode())
    return "sha256:" + h.hexdigest()


def _repo_loc(root: str, extra_ignore=()) -> int:
    total = 0
    for path in _iter_python_files(root, extra_ignore):
        with open(path, encoding="utf-8", errors="replace") as fh:
            total += sum(1 for _ in fh)
    return total


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", name.strip().lower()).strip("_")
    return s or "node"


def _save_run(root: str, provider_name: str, node_count, result: dict) -> str:
    """Сырой ответ модели в ``.mindgraph/runs/`` — аудит/отладка группировки:
    если модель прислала невалидный JSON или расклад "поплыл", видно, что
    именно она вернула, без повторного платного вызова.
    """
    runs_dir = os.path.join(root, ".mindgraph", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    k = "auto" if node_count is None else str(node_count)
    path = os.path.join(runs_dir, f"{ts}_{provider_name}_k{k}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "provider": provider_name,
            "node_count": node_count,
            "raw_text": result["raw_text"],
            "usage": result["usage"],
            "warnings": result["warnings"],
            "unassigned_count": result["unassigned_count"],
        }, fh, ensure_ascii=False, indent=2)
    return path


def _save_domain_run(root: str, provider_name: str, result: dict) -> str:
    """Как :func:`_save_run`, но для второго (доменного) запроса — своя
    директория, чтобы не перепутать сырые ответы двух разных промптов."""
    runs_dir = os.path.join(root, ".mindgraph", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    path = os.path.join(runs_dir, f"{ts}_{provider_name}_domains.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "provider": provider_name,
            "raw_text": result["raw_text"],
            "usage": result["usage"],
            "warnings": result["warnings"],
            "domains": result["domains"],
        }, fh, ensure_ascii=False, indent=2)
    return path


def _has_llm_grouping(graph: dict) -> bool:
    """Есть ли в ``graph.json`` узлы от модели (а не package-proxy).

    Явное поле ``grouping`` — признак, который пишется с этой версии. Для
    graph.json без него (версия ≤2) — эвристика: package-proxy оставляет
    ``summary`` узлов пустыми, LLM-группировка их заполняет.
    """
    if graph.get("grouping") == "llm":
        return True
    if graph.get("grouping") == "package":
        return False
    return any(n.get("summary") for n in graph.get("nodes", []))


def _preserved_grouping(graph: dict):
    """Извлечь LLM-разметку существующего graph.json для переноса при --no-llm.

    Возвращает ``(node_of_symbol, node_summary, node_parent, domains)`` —
    ровно то, что надо пересохранить, чтобы пересчёт статики не затирал
    смысловые узлы модели (см. :func:`build_graph`, параметр ``preserve_from``).
    """
    symbol_node = {}
    for s in graph.get("symbols", []):
        if s.get("node"):
            symbol_node[s["id"]] = s["node"]
    node_summary = {}
    node_parent = {}
    for n in graph.get("nodes", []):
        node_summary[n["name"]] = n.get("summary", "")
        if n.get("parent"):
            node_parent[n["name"]] = n["parent"]
    return symbol_node, node_summary, node_parent, list(graph.get("domains", []))


def _restore_domains(out_nodes: list, preserved_parent: dict,
                     preserved_domains: list) -> list:
    """Вернуть домены, сохранив прежний ``parent`` у существующих узлов L1.

    Новые узлы (символы, появившиеся после изменения кода) уходят в служебный
    домен :data:`UNASSIGNED_DOMAIN` до следующего ``--regroup``.
    """
    valid = {d["id"] for d in preserved_domains}
    fallback_id = "d_" + _slug(UNASSIGNED_DOMAIN)
    need_fallback = False
    for n in out_nodes:
        pid = preserved_parent.get(n["name"])
        if pid and pid in valid:
            n["parent"] = pid
        else:
            n["parent"] = fallback_id
            need_fallback = True
    domains = list(preserved_domains)
    if need_fallback:
        domains.append({"id": fallback_id, "name": UNASSIGNED_DOMAIN})
    return domains


def build_graph(root: str, provider_name: str = "deepseek", no_llm: bool = False,
                repo_hash: str | None = None,
                preserve_from: dict | None = None,
                extra_ignore=(),
                model: str | None = None,
                cache_base=None,
                entry_points=None) -> dict:
    """Extract -> (group) -> shared -> единый graph.json (в памяти, без записи).

    Если LLM-провайдер недоступен (нет ключа/пакета) и ``no_llm`` не
    выставлен явно — тихо откатываемся на package-proxy (см. shared.py) и
    пишем предупреждение в graph.warnings, а не падаем (§1 спеки: никогда не
    падать целиком).

    ``preserve_from`` — существующий graph.json с LLM-разметкой. Если задан,
    LLM-вызов не делается вообще: статика (символы/рёбра/shared/статы)
    пересчитывается заново, а назначение символов по узлам, summary и домены
    берутся из него. Так ``--no-llm``/обычный пересчёт никогда не затирает
    LLM-разметку; полная перегруппировка — только отдельным ``--regroup``.

    ``model`` — переопределение дефолтной модели провайдера; ``cache_base`` —
    каталог кэша LLM-ответов (см. llmcache.py); ``entry_points`` — glob-паттерны
    точек входа (по умолчанию ENTRY_POINT_PATTERNS).
    """
    root = os.path.abspath(root)
    extracted = extract_repo(root, extra_ignore)
    symbols, symbol_edges = extracted["symbols"], extracted["symbol_edges"]
    warnings = list(extracted["warnings"])
    entry_patterns = ENTRY_POINT_PATTERNS if entry_points is None else entry_points

    node_of_symbol: dict = {}
    node_meta: dict = {}
    unassigned = 0
    grouping = "package"
    preserved_parent: dict = {}
    preserved_domains: list = []

    if preserve_from is not None:
        preserved_node, preserved_summary, preserved_parent, preserved_domains = \
            _preserved_grouping(preserve_from)
        for s in symbols:
            node_of_symbol[s["id"]] = (
                preserved_node.get(s["id"]) or node_of(s["file"], "package"))
        node_meta = {name: preserved_summary.get(name, "")
                     for name in set(node_of_symbol.values())}
        unassigned = sum(1 for s in symbols
                         if not s.get("synthetic")
                         and node_of_symbol[s["id"]] == UNASSIGNED_NODE)
        grouping = "llm"
    elif not no_llm:
        try:
            provider = make_provider(provider_name) if model is None \
                else make_provider(provider_name, model=model)
        except Exception as e:  # нет ключа, нет пакета, и т.п. — не фатально
            warnings.append(
                f"LLM-провайдер {provider_name!r} недоступен ({e}); работаю как --no-llm")
            provider = None
        if provider is not None:
            result = group_repo(symbols, symbol_edges, provider, cache_base=cache_base)
            _save_run(root, provider_name, None, result)
            warnings.extend(result["warnings"])
            node_of_symbol = node_map(result["nodes"])
            node_meta = {n["name"]: n.get("summary", "") for n in result["nodes"]}
            unassigned = result["unassigned_count"]
            grouping = "llm"

    if not node_of_symbol:  # --no-llm или провайдер недоступен
        node_of_symbol = {s["id"]: node_of(s["file"], "package") for s in symbols}
        node_meta = {name: "" for name in set(node_of_symbol.values())}
        grouping = "package"

    entry_ids = {s["id"] for s in symbols if _is_entry_point(s["file"], entry_patterns)}
    direct = compute_owners_by_node(symbols, symbol_edges, node_of_symbol, entry_ids=entry_ids)

    out_symbols = []
    for s in symbols:
        o = direct.get(s["id"], {"node": node_of_symbol.get(s["id"]), "owners": [], "shared": False})
        entry = dict(s)
        entry.update({"node": o["node"], "shared": o["shared"], "owners": o["owners"]})
        out_symbols.append(entry)

    measured = [s for s in symbols if not s.get("synthetic")]
    shared_symbols = [s for s in measured if direct[s["id"]]["shared"]]
    shared_ratio = (len(shared_symbols) / len(measured)) if measured else 0.0

    by_node: dict = {}
    for s in symbols:
        by_node.setdefault(node_of_symbol[s["id"]], []).append(s)

    out_nodes = []
    for name, members in sorted(by_node.items()):
        nid = "n_" + _slug(name)
        shared_count = sum(1 for m in members
                           if not m.get("synthetic") and direct[m["id"]]["shared"])
        out_nodes.append({
            "id": nid, "name": name, "summary": node_meta.get(name, ""),
            "symbols": sorted(m["id"] for m in members),
            "files": sorted({m["file"] for m in members}),
            "loc": sum(m["loc"] for m in members),
            "shared_count": shared_count,
        })
    node_id_of_name = {n["name"]: n["id"] for n in out_nodes}

    agg: dict = {}
    for e in symbol_edges:
        a = node_id_of_name.get(node_of_symbol.get(e["from"]))
        b = node_id_of_name.get(node_of_symbol.get(e["to"]))
        if a is None or b is None or a == b:
            continue
        agg[(a, b)] = agg.get((a, b), 0) + e["weight"]
    out_node_edges = [{"from": a, "to": b, "weight": w, "weak": w == 1}
                      for (a, b), w in sorted(agg.items())]

    # Этап B2 (domains.py) — ОТДЕЛЬНЫЙ второй запрос к модели, группирующий
    # уже готовые узлы L1 в домены L0. Символы туда не передаются вообще, и
    # assign_domains не должен их трогать — assert ниже это гарантирует:
    # если разметка символов по узлам L1 хоть на один id разъехалась
    # до/после вызова, эта сборка graph.json обязана упасть, а не тихо
    # разъехаться с тем, что уже проверено на этапе B (§3 спеки).
    if preserve_from is not None:
        domains = _restore_domains(out_nodes, preserved_parent, preserved_domains)
    else:
        before_symbols = {n["id"]: set(n["symbols"]) for n in out_nodes}
        domains_result = assign_domains(out_nodes, out_node_edges,
                                        provider_name=provider_name, no_llm=no_llm,
                                        cache_base=cache_base, model=model)
        if domains_result["raw_text"]:
            _save_domain_run(root, provider_name, domains_result)
        warnings.extend(domains_result["warnings"])
        after_symbols = {n["id"]: set(n["symbols"]) for n in out_nodes}
        assert before_symbols == after_symbols, (
            "assign_domains изменил множество символов хотя бы одного узла L1 — "
            "инвариант нарушен (домены не должны трогать назначение символов)")
        domains = domains_result["domains"]

    domain_id_of_node_id = {n["id"]: n.get("parent") for n in out_nodes}
    dagg: dict = {}
    for e in out_node_edges:
        da, db = domain_id_of_node_id.get(e["from"]), domain_id_of_node_id.get(e["to"])
        if da is None or db is None or da == db:
            continue
        dagg[(da, db)] = dagg.get((da, db), 0) + e["weight"]
    out_domain_edges = [{"from": a, "to": b, "weight": w, "weak": w == 1}
                        for (a, b), w in sorted(dagg.items())]

    return {
        "version": GRAPH_VERSION,
        "grouping": grouping,
        "repo": {
            "root": root,
            "name": os.path.basename(root.rstrip("/")) or root,
            "repo_hash": repo_hash or compute_repo_hash(root, extra_ignore),
            "files": len({s["file"] for s in symbols}),
            "loc": _repo_loc(root, extra_ignore),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "symbols": sorted(out_symbols, key=lambda s: s["id"]),
        "symbol_edges": symbol_edges,
        "nodes": out_nodes,
        "node_edges": out_node_edges,
        "domains": domains,
        "domain_edges": out_domain_edges,
        "stats": {
            "symbols": len(measured),
            "unassigned": unassigned,
            "shared_symbols": len(shared_symbols),
            "shared_ratio": shared_ratio,
            "external": extracted["external"],
        },
        "warnings": warnings,
    }


def write_graph(root: str, graph: dict) -> str:
    path = graph_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=2)
    return path


def load_or_build(root: str, provider_name: str = "deepseek", no_llm: bool = False,
                  force: bool = False, regroup: bool = False, extra_ignore=(),
                  model: str | None = None, cache_base=None, entry_points=None):
    """Вернуть ``(graph, from_cache: bool)``. Пишет на диск при пересчёте.

    Cache-first: если ``repo_hash`` в ``.mindgraph/graph.json`` совпал с текущим
    и нет ``--force``/``--regroup`` — возвращаем кэш без пересчёта. Защита
    LLM-разметки (§6): ``--no-llm`` (и обычный пересчёт при изменившемся хэше
    без ``--regroup``) пересчитывает только статику, а существующие узлы модели
    оставляет на месте. Полный пересчёт — только ``--regroup`` (и он бесплатен
    при неизменном коде — кэш LLM-ответов по хэшу промпта).
    """
    root = os.path.abspath(root)
    current_hash = compute_repo_hash(root, extra_ignore)
    path = graph_path(root)

    existing = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = None  # повреждённый кэш — пересчитываем молча

    # --regroup: полный пересчёт LLM-разметки, всегда, даже при актуальном
    # кэше (это и есть смысл флага — явная перегруппировка).
    if regroup:
        graph = build_graph(root, provider_name=provider_name, no_llm=False,
                            repo_hash=current_hash, extra_ignore=extra_ignore,
                            model=model, cache_base=cache_base,
                            entry_points=entry_points)
        write_graph(root, graph)
        return graph, False

    # Кэш актуален и --force не задан — переиспользуем без пересчёта.
    if not force and existing is not None \
            and existing.get("repo", {}).get("repo_hash") == current_hash:
        return existing, True

    # Пересчёт статики. Если в graph.json уже есть LLM-узлы — переносим их
    # (никогда не затираем); полная перегруппировка — только через --regroup.
    preserve_from = existing if (existing is not None and _has_llm_grouping(existing)) else None

    if preserve_from is not None:
        graph = build_graph(root, provider_name=provider_name, no_llm=True,
                            repo_hash=current_hash, preserve_from=preserve_from,
                            extra_ignore=extra_ignore, model=model,
                            cache_base=cache_base, entry_points=entry_points)
    else:
        graph = build_graph(root, provider_name=provider_name, no_llm=no_llm,
                            repo_hash=current_hash, extra_ignore=extra_ignore,
                            model=model, cache_base=cache_base,
                            entry_points=entry_points)

    write_graph(root, graph)
    return graph, False
