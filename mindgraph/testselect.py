"""testselect.py — какие тесты прогонять (фаза 1, команда ``tests``).

Эвристика, механическая: тестовым считается файл в каталоге ``tests/``, или
``test_*.py``/``*_test.py``/``tests.py``. Тест относится к узлу/диффу, если
какой-то его символ ссылается (рёбрами) на символы узла/изменённых файлов.

Фаза 1.5: выбор тестов честно сообщает о своей уверенности. Три вердикта —
``ok`` / ``failed`` / ``unverified`` (см. model.STATUS_*): пустой или
низкоуверенный выбор — это ``unverified`` с причиной, а не «зелёный» успех.
Нерезолвленные импорты снижают ``selection_confidence``.
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
            "reason": "не удалось выбрать тесты: тестовые файлы не ссылаются "
                      "на символы запроса",
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
