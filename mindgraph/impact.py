"""impact.py — the transitive set of "what an edit to a symbol/file touches".

Forward traversal over ``symbol_edges`` (from -> to): starting from a symbol (or
all symbols of a file) we walk calls/mentions/inheritance forward and collect
everything reachable. ``depth`` limits the number of edges (None — unlimited).
"""

from __future__ import annotations

from . import model


def _start_symbols(graph: dict, query: str) -> set:
    """Starting set: a symbol by id, or all symbols of a file."""
    if query in {s["id"] for s in graph["symbols"]}:
        return {query}
    return {s["id"] for s in graph["symbols"] if s["file"] == query}


def impact(graph: dict, query: str, depth: int | None = None) -> dict:
    """Returns affected symbols and files + incompleteness (unresolved imports).

    ``complete=False`` means: the graph has unresolved imports, part of the
    affected set may have been missed (impact is understated).
    """
    start = _start_symbols(graph, query)
    if not start:
        return {
            "query": query,
            "depth": depth,
            "symbols": [],
            "files": [],
            "unresolved": model.unresolved_imports(graph),
            "complete": not model.graph_incomplete(graph),
        }

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
        "unresolved": model.unresolved_imports(graph),
        "complete": not model.graph_incomplete(graph),
    }
