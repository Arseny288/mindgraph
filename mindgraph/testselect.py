"""testselect.py — which tests to run (phase 1, the ``tests`` command).

Heuristic, mechanical: a test file is one in a ``tests/`` directory, or
``test_*.py``/``*_test.py``/``tests.py``. A test relates to a node/diff if one of
its symbols references (by edges) symbols of the node/changed files.

Phase 1.5: test selection honestly reports its confidence. Three verdicts —
``ok`` / ``failed`` / ``unverified`` (see model.STATUS_*): an empty or
low-confidence selection is ``unverified`` with a reason, not a "green" success.
Unresolved imports lower ``selection_confidence``.
"""

from __future__ import annotations

from . import model


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1]
    return ("tests" in parts
            or base.startswith("test_")
            or base.endswith("_test.py")
            or base == "tests.py")


def _symbol_by_id(graph: dict) -> dict:
    return {s["id"]: s for s in graph["symbols"]}


def _tests_referencing(graph: dict, target_symbols: set) -> list:
    sym_by_id = _symbol_by_id(graph)
    tests = set()
    for e in graph["symbol_edges"]:
        if e["to"] not in target_symbols:
            continue
        src = sym_by_id.get(e["from"])
        if src is not None and is_test_file(src["file"]):
            tests.add(src["file"])
    return sorted(tests)


def _wrap(graph: dict, tests: list, query: str) -> dict:
    unresolved = model.unresolved_imports(graph)
    if not tests:
        return {
            "schema_version": "1",
            "status": model.STATUS_UNVERIFIED,
            "reason": "could not select tests: no test files reference "
                      "the symbols of the query",
            "tests": [],
            "selection_confidence": 0.0,
            "unresolved_imports": unresolved,
            "query": query,
        }
    confidence = 1.0 if not unresolved else 0.5
    status = model.STATUS_OK if confidence >= 0.5 else model.STATUS_UNVERIFIED
    return {
        "schema_version": "1",
        "status": status,
        "tests": tests,
        "selection_confidence": confidence,
        "unresolved_imports": unresolved,
        "query": query,
    }


def node_tests(graph: dict, node: dict, query: str) -> dict:
    owned = {s["id"] for s in graph["symbols"]
             if s["node"] == node["name"] and not s.get("synthetic")}
    return _wrap(graph, _tests_referencing(graph, owned), query)


def diff_tests(graph: dict, changed_paths, query: str) -> dict:
    changed = {s["id"] for s in graph["symbols"]
               if s["file"] in set(changed_paths)}
    return _wrap(graph, _tests_referencing(graph, changed), query)
