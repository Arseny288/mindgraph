"""impact.py — транзитивное множество «что заденет правка символа/файла».

Прямой обход по ``symbol_edges`` (from -> to): от символа (или всех символов
файла) идём по вызовам/упоминаниям/наследованию вперёд и собираем всё
достижимое. ``depth`` ограничивает число рёбер (None — без ограничения).
"""

from __future__ import annotations


def _start_symbols(graph: dict, query: str) -> set:
    """Начальное множество: символ по id, либо все символы файла."""
    if query in {s["id"] for s in graph["symbols"]}:
        return {query}
    return {s["id"] for s in graph["symbols"] if s["file"] == query}


def impact(graph: dict, query: str, depth: int | None = None) -> dict:
    """Возвращает затронутые символы и файлы (плюс рёбра затронутого подграфа)."""
    start = _start_symbols(graph, query)
    if not start:
        return {"query": query, "symbols": [], "files": []}

    outgoing: dict = {}
    for e in graph["symbol_edges"]:
        outgoing.setdefault(e["from"], set()).add(e["to"])

    reachable = set(start)
    frontier = set(start)
    d = 0
    while frontier and (depth is None or d < depth):
        nxt = set()
        for sid in frontier:
            nxt |= outgoing.get(sid, set())
        nxt -= reachable
        reachable |= nxt
        frontier = nxt
        d += 1

    sym_by_id = {s["id"]: s for s in graph["symbols"]}
    symbols = sorted(sid for sid in reachable if sid in sym_by_id)
    files = sorted({sym_by_id[sid]["file"] for sid in symbols})

    return {
        "query": query,
        "depth": depth,
        "symbols": symbols,
        "files": files,
    }
