"""verify.py — механическая проверка диффа против границы узла (фаза 1.3, 1.5).

Без LLM. Проверяет ДО символов, по кумулятивному диффу от базового коммита
сессии (не по приросту одного хода — иначе диапазоны строк «едут» после первой
правки):

* ``outside_node_zone`` — дифф трогает файл вне ``allow_write`` узла;
* ``foreign_symbol_in_shared_file`` — изменён символ чужого узла в смешанном
  файле зоны (изменение тела/сигнатуры либо удаление символа целиком);
* ``contract_signature_changed`` — изменена сигнатура контрактного (shared)
  символа, которым владеет сам узел;
* ``unattributed_change`` — строки файла зоны, не принадлежащие ни одному
  символу (импорты, код верхнего уровня): НЕ нарушение, но и не тихий ``ok``.

Атрибуция новой стороны диффа — переизвлечением символов из ИЗМЕНЁННЫХ ФАЙЛОВ
(``original_dir`` — корень worktree; там уже применён кумулятивный дифф), а не
по ``symbols[].lines`` из graph.json (они относятся к базовому состоянию).
"""

from __future__ import annotations

import ast
import os
import re

from . import model
from .extract import _collect_symbols, _module_id

try:
    import pathspec
except ImportError:
    pathspec = None


def _normalize_path(raw: str) -> str | None:
    """Чистый относительный путь (прямые слэши), либо None, если вне репо."""
    p = raw.strip()
    if p == "/dev/null":
        return None
    p = re.sub(r"^[ab]/", "", p)
    p = p.replace("\\", "/")
    if p.startswith("/") or ".." in p.split("/"):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    return "/".join(parts) if parts else None


def _matches_globs(path: str, globs) -> bool:
    if not globs:
        return False
    if pathspec is not None:
        spec = pathspec.PathSpec.from_lines("gitignore", list(globs))
        return spec.match_file(path)
    for g in globs:
        if g.endswith("/**"):
            if path == g[:-3] or path.startswith(g[:-3] + "/"):
                return True
        elif path == g:
            return True
    return False


def _parse_diff(text: str) -> list:
    """Разбить unified diff на файлы: ``[{path, is_new, is_deleted, hunks}]``."""
    files = []
    cur = None
    for line in text.splitlines():
        if line.startswith("diff --git ") or line.startswith("Index: "):
            cur = {"path": None, "is_new": False, "is_deleted": False, "hunks": []}
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("+++ "):
            raw = line[4:]
            cur["is_new"] = "/dev/null" in raw
            cur["path"] = _normalize_path(raw)
        elif line.startswith("--- "):
            cur["is_deleted"] = "/dev/null" in line[4:]
        elif line.startswith("@@"):
            cur["hunks"].append({"header": line, "lines": []})
        elif cur["hunks"] and line:
            cur["hunks"][-1]["lines"].append(line)
    return [f for f in files if f["path"]]


def _hunk_line_ranges(hunks: list):
    """Разобрать хунки на (added_new, removed_base) — номера ИЗМЕНЁННЫХ строк.

    Только строки ``+`` (в координатах новой стороны) и ``-`` (в координатах
    базовой стороны); контекстные строки не считаются изменениями.
    """
    added = []
    removed = []
    for hunk in hunks:
        m = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?",
                      hunk["header"])
        if not m:
            continue
        base_line = int(m.group(1))
        new_line = int(m.group(3))
        for ln in hunk["lines"]:
            if ln.startswith("+"):
                added.append(new_line)
                new_line += 1
            elif ln.startswith("-"):
                removed.append(base_line)
                base_line += 1
            elif ln.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:  # контекст
                base_line += 1
                new_line += 1
    return added, removed


def _extract_file_symbols(path: str, text: str) -> dict:
    """Переизвлечь РЕАЛЬНЫЕ символы файла: ``{id: {lines, sig}}`` (новая сторона)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    syms = _collect_symbols(_module_id(path), path, tree)
    return {s["id"]: {"lines": s["lines"], "sig": s["sig"]}
            for s in syms if not s.get("synthetic")}


def _overlaps_lines(lines, points) -> bool:
    lo, hi = lines[0], lines[1]
    return any(lo <= p <= hi for p in points)


def _collapse_points(points) -> list:
    """Свернуть отсортированные номера строк в интервалы ``[[a, b], ...]``."""
    if not points:
        return []
    points = sorted(set(points))
    out = [[points[0], points[0]]]
    for p in points[1:]:
        if p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return out


def _read_file(root: str | None, path: str) -> str:
    if root is None:
        return ""
    p = os.path.join(root, path)
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def verify(graph: dict, node: dict, diff_text: str,
           original_dir: str | None = None) -> dict:
    """Проверить кумулятивный дифф против границы узла.

    ``original_dir`` — корень worktree (уже содержит новую сторону диффа).
    """
    bnd = model.boundary(graph, node)
    owners = model.file_owner_map(graph)
    base_syms = {s["id"]: s for s in graph["symbols"]
                 if not s.get("synthetic")}

    violations = []
    unattributed = []
    changed_paths = set()

    for f in _parse_diff(diff_text):
        path = f["path"]
        if path is None or f["is_deleted"]:
            continue
        changed_paths.add(path)

        if not _matches_globs(path, bnd["allow_write"]):
            violations.append({
                "path": path,
                "reason": "outside_node_zone",
                "owner": owners.get(path, "?"),
            })
            continue

        new_text = _read_file(original_dir, path)
        new_syms = _extract_file_symbols(path, new_text)
        added, removed = _hunk_line_ranges(f["hunks"])

        base_in_file = {sid: s for sid, s in base_syms.items()
                        if s["file"] == path}

        # 1. Удалённый символ чужого узла (нет в новой стороне).
        for sid, base in base_in_file.items():
            if sid not in new_syms and base["node"] != node["name"]:
                violations.append({
                    "symbol": sid, "path": path,
                    "reason": "foreign_symbol_in_shared_file",
                    "owner": base["node"],
                })

        # 2. Изменённый символ (сигнатура или тело).
        for sid, ns in new_syms.items():
            base = base_in_file.get(sid)
            if base is None:
                continue  # новый символ — код самого узла
            sig_changed = model.signature_hash(ns["sig"]) \
                != model.signature_hash(base["sig"])
            body_touched = _overlaps_lines(ns["lines"], added) \
                or _overlaps_lines(base["lines"], removed)
            if base["node"] != node["name"] and (sig_changed or body_touched):
                violations.append({
                    "symbol": sid, "path": path,
                    "reason": "foreign_symbol_in_shared_file",
                    "owner": base["node"],
                })
            elif base["node"] == node["name"] and base.get("shared") \
                    and sig_changed:
                violations.append({
                    "symbol": sid,
                    "reason": "contract_signature_changed",
                })

        # 3. Изменённые строки, не приписанные существующему символу:
        #    новые символы (нет базового владельца) и код вне символов.
        attributed = [ns["lines"] for sid, ns in new_syms.items()
                      if sid in base_in_file]
        uncovered = [p for p in added
                     if not any(lo <= p <= hi for lo, hi in attributed)]
        if uncovered:
            unattributed.append({"path": path,
                                 "lines": _collapse_points(uncovered)})

    result = {
        "schema_version": "1",
        "ok": not violations,
        "changed": sorted(changed_paths),
        "violations": violations,
        "unattributed_change": unattributed,
        "unresolved": model.unresolved_imports(graph),
        "complete": not model.graph_incomplete(graph),
    }
    return result
