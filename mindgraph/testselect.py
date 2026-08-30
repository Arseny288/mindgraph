"""testselect.py — какие тесты прогонять (фаза 1, команда ``tests``).

Эвристика, механическая: тестовым считается файл в каталоге ``tests/``, или
``test_*.py``/``*_test.py``/``tests.py``. Тест относится к узлу/диффу, если
какой-то его символ ссылается (рёбрами) на символы узла/изменённых файлов.
"""

from __future__ import annotations


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


def node_tests(graph: dict, node: dict) -> list:
    owned = {s["id"] for s in graph["symbols"]
             if s["node"] == node["name"] and not s.get("synthetic")}
    return _tests_referencing(graph, owned)


def diff_tests(graph: dict, changed_paths) -> list:
    changed = {s["id"] for s in graph["symbols"]
               if s["file"] in set(changed_paths)}
    return _tests_referencing(graph, changed)
