"""verify.py — механическая проверка диффа против границы узла (фаза 1.3).

Без LLM. Два класса нарушений:

* ``outside_node_zone`` — дифф трогает файл вне ``allow_write`` узла (по путям);
* ``contract_signature_changed`` — файл в зоне, но изменилась сигнатура
  контрактного символа (сверка по ``sha256(sig)``).

Пути нормализуются: срезаются git-префиксы ``a/``/``b/``, ``..`` и абсолютные
пути за пределы репо отсекаются (считаются нарушением).
"""

from __future__ import annotations

import ast
import hashlib
import os
import re

from . import model
from .extract import _module_id

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
    # без pathspec — простое префиксное/суффиксное совпадение для «x/**».
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


def _apply_diff(original: str, hunks: list) -> str:
    """Реконструировать новое содержимое файла по хункам (best-effort)."""
    orig_lines = original.splitlines()
    out = []
    pos = 0  # индекс в orig_lines (0-based)
    for hunk in hunks:
        m = re.search(r"@@ -(\d+)(?:,(\d+))?", hunk["header"])
        if not m:
            continue
        old_start = int(m.group(1)) - 1  # 0-based
        if old_start < pos:
            old_start = pos
        out.extend(orig_lines[pos:old_start])
        pos = old_start
        for ln in hunk["lines"]:
            if ln.startswith(" "):
                if pos < len(orig_lines):
                    out.append(orig_lines[pos])
                    pos += 1
            elif ln.startswith("-"):
                pos += 1  # удаляем строку оригинала
            elif ln.startswith("+"):
                out.append(ln[1:])
            elif ln == "\\ No newline at end of file":
                pass
    out.extend(orig_lines[pos:])
    return "\n".join(out)


def _local_signatures(text: str) -> dict:
    """``{локальное_имя: сигнатура}`` для top-level def/class + методы."""
    from .extract import _signature
    sigs = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return sigs
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs[node.name] = _signature(node)
        elif isinstance(node, ast.ClassDef):
            sigs[node.name] = _signature(node)
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sigs[f"{node.name}.{m.name}"] = _signature(m)
    return sigs


def verify(graph: dict, node: dict, diff_text: str,
           original_dir: str | None = None) -> dict:
    """Проверить дифф против границы узла. Возвращает вердикт + нарушения."""
    bnd = model.boundary(graph, node)
    owners = model.file_owner_map(graph)
    sym_by_id = {s["id"]: s for s in graph["symbols"]}

    # ВСЕ контрактные (shared) символы — их сигнатуру нельзя менять, даже если
    # файл в зоне узла (символ общий, сломает другие узлы).
    contracts_by_file: dict = {}
    for c in model.contract_symbols(graph):
        target = sym_by_id.get(c["symbol"])
        if target is not None:
            contracts_by_file.setdefault(target["file"], []).append(c)

    violations = []
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

        # Файл в зоне — проверить, не изменилась ли сигнатура контракта.
        new_text = _apply_diff(_read_original(original_dir, path), f["hunks"])
        sigs = _local_signatures(new_text)
        for c in contracts_by_file.get(path, []):
            target = sym_by_id[c["symbol"]]
            local = c["symbol"][len(_module_id(path)) + 1:] \
                if c["symbol"].startswith(_module_id(path) + ".") else c["symbol"]
            new_sig = sigs.get(local)
            if model.signature_hash(new_sig) != c["signature_hash"]:
                violations.append({
                    "symbol": c["symbol"],
                    "reason": "contract_signature_changed",
                })

    return {
        "schema_version": "1",
        "ok": not violations,
        "changed": sorted(changed_paths),
        "violations": violations,
    }


def _read_original(root: str | None, path: str) -> str:
    if root is None:
        return ""
    p = os.path.join(root, path)
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""
