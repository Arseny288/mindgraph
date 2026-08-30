"""model.py — модель владения и контрактов поверх graph.json (фаза 1).

Отвечает на вопросы «что узел может править», «что он читает по контракту»,
«что ему недоступно». Всё механически, без LLM:

* символ -> узел: поле ``symbols[].node`` (уже посчитано в graph.json);
* файл -> узел-хозяин: большинство РЕАЛЬНЫХ символов файла (tie-break —
  лексикографически по имени узла); файл без реальных символов наследует узел
  своего псевдо-``<module>``;
* контракт: символ ``shared == true`` (его используют более чем из одного
  узла); его сигнатура фиксируется хэшем ``sha256(sig)`` и не может меняться
  без явного согласования.

Всё это — read-only анализ уже собранного ``graph.json``; ничего не
досчитывает из исходников и не ходит в LLM.
"""

from __future__ import annotations

import hashlib
import json
import os


def load_graph(root: str) -> dict:
    """Прочитать ``<root>/.mindgraph/graph.json`` (контракт emit.py)."""
    path = os.path.join(os.path.abspath(root), ".mindgraph", "graph.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def signature_hash(sig: str | None) -> str:
    """Фиксированный хэш сигнатуры контракта."""
    return "sha256:" + hashlib.sha256((sig or "").encode("utf-8")).hexdigest()[:16]


def node_by_ref(graph: dict, ref: str) -> dict:
    """Найти узел L1 по id или по имени (принимает и то, и то)."""
    for n in graph["nodes"]:
        if n["id"] == ref or n["name"] == ref:
            return n
    raise KeyError(f"узел {ref!r} не найден в graph.json")


def _symbol_by_id(graph: dict) -> dict:
    return {s["id"]: s for s in graph["symbols"]}


def file_owner_map(graph: dict) -> dict:
    """``{file: node_name}`` — узел-хозяин каждого файла (большинство символов)."""
    counts: dict = {}
    for s in graph["symbols"]:
        if s.get("synthetic"):
            continue
        counts.setdefault(s["file"], {})
        counts[s["file"]][s["node"]] = counts[s["file"]].get(s["node"], 0) + 1

    owners: dict = {}
    for f, c in counts.items():
        # max по числу символов; на равенстве — лексикографически первый узел.
        owners[f] = max(sorted(c), key=lambda n: c[n])

    # Файлы без реальных символов: наследуют узел своего <module>.
    for s in graph["symbols"]:
        if s.get("synthetic") and s["file"] not in owners:
            owners[s["file"]] = s["node"]
    return owners


def contract_symbols(graph: dict) -> list:
    """Все контрактные (shared) символы с фиксированной сигнатурой."""
    return [{"symbol": s["id"], "node": s["node"],
             "signature_hash": signature_hash(s["sig"])}
            for s in graph["symbols"]
            if s.get("shared") and not s.get("synthetic")]


def node_contracts(graph: dict, node: dict) -> list:
    """Контракты, которые узел ИСПОЛЬЗУЕТ (зависит), но не владеет.

    Ребро ``from`` принадлежит символу узла, ``to`` — shared-символу другого
    узла. Читать можно, менять сигнатуру нельзя.
    """
    sym_by_id = _symbol_by_id(graph)
    owned = {s["id"] for s in graph["symbols"]
             if s["node"] == node["name"] and not s.get("synthetic")}
    used: dict = {}
    for e in graph["symbol_edges"]:
        if e["from"] not in owned:
            continue
        target = sym_by_id.get(e["to"])
        if target is None or not target.get("shared") \
                or target["node"] == node["name"]:
            continue
        used[e["to"]] = target
    return [{"symbol": sid, "node": sym_by_id[sid]["node"],
             "signature_hash": signature_hash(sym_by_id[sid]["sig"])}
            for sid in sorted(used)]


def node_view(graph: dict, node: dict) -> dict:
    """Всё, что нужно команде ``node``: owned / contracts / foreign."""
    owners = file_owner_map(graph)
    node_files = set(node.get("files", []))
    owned_files = sorted(f for f, owner in owners.items()
                         if owner == node["name"] and f in node_files)
    owned_symbols = sorted(
        (s for s in graph["symbols"]
         if s["node"] == node["name"] and not s.get("synthetic")),
        key=lambda s: s["id"])
    contracts = node_contracts(graph, node)

    all_files = {s["file"] for s in graph["symbols"]}
    foreign_files = sorted(all_files - set(owned_files))

    return {
        "owned_files": owned_files,
        "owned_symbols": [s["id"] for s in owned_symbols],
        "contracts": contracts,
        "foreign_files": foreign_files,
    }


def allow_write_globs(graph: dict, node: dict) -> list:
    """Разрешения на запись: glob каталога, если каталог ЦЕЛИКОМ принадлежит
    узлу, иначе — точные пути файлов (каталог общий, «dir/**» перехватил бы
    чужие файлы). Плюс зеркальные каталоги тестов."""
    owners = file_owner_map(graph)
    all_files = {s["file"] for s in graph["symbols"]}
    owned = sorted(f for f, owner in owners.items() if owner == node["name"])

    dir_of: dict = {}
    for f in all_files:
        dir_of.setdefault(os.path.dirname(f), set()).add(f)

    by_dir: dict = {}
    for f in owned:
        by_dir.setdefault(os.path.dirname(f), []).append(f)

    entries = set()
    for d, fs in sorted(by_dir.items()):
        if set(fs) == dir_of.get(d, set()):
            entries.add((d + "/**") if d else "**")
        else:
            entries.update(fs)  # каталог делят несколько узлов — точечно

    # Зеркальные тесты: каталогу `X/sub` соответствует `tests/sub/**`.
    extra = set()
    for f in owned:
        parts = f.split("/")
        for i in range(1, len(parts)):
            cand = "tests/" + "/".join(parts[i:])
            if any(f2 == cand or f2.startswith(cand + "/")
                   for f2 in all_files):
                extra.add(cand + "/**")
                break
    return sorted(entries | extra)


def boundary(graph: dict, node: dict) -> dict:
    """Границы зоны узла: allow_write / deny_write / allow_read / contracts.

    Порядок разрешения — deny по умолчанию (``**``), allow точечно.
    ``allow_read`` = зона записи + ТОЧНЫЕ пути файлов контрактных символов
    (читать можно, менять сигнатуру нельзя).
    """
    contracts = node_contracts(graph, node)
    sym_by_id = _symbol_by_id(graph)

    allow_write = allow_write_globs(graph, node)
    contract_files = sorted({sym_by_id[c["symbol"]]["file"]
                             for c in contracts
                             if c["symbol"] in sym_by_id})
    allow_read = sorted(set(allow_write) | set(contract_files))

    return {
        "schema_version": "1",
        "node_id": node["id"],
        "node_name": node["name"],
        "allow_write": allow_write,
        "deny_write": ["**"],
        "allow_read": allow_read,
        "contracts": contracts,
    }
