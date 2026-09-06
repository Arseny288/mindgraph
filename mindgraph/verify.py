"""verify.py — mechanical check of a diff against a node's boundary (phases 1.3, 1.5).

No LLM. Checks down to symbols, over the cumulative diff from the session's base
commit (not by the increment of a single move — otherwise line ranges "drift"
after the first edit):

* ``outside_node_zone`` — the diff touches a file outside the node's ``allow_write``;
* ``foreign_symbol_in_shared_file`` — a foreign node's symbol in a mixed file of
  the zone was changed (body/signature change or full deletion of the symbol);
* ``contract_signature_changed`` — the signature of a contract (shared) symbol
  owned by the node itself was changed;
* ``unattributed_change`` — lines of a zone file not belonging to any symbol
  (imports, top-level code): NOT a violation, but not a silent ``ok`` either.

Attribution of the diff's new side — by re-extracting symbols from the CHANGED
FILES (``original_dir`` — the worktree root; the cumulative diff is already
applied there), not by ``symbols[].lines`` from graph.json (those refer to the
base state).
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
    """A clean relative path (forward slashes), or None if outside the repo."""
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
    """Split a unified diff into files: ``[{path, is_new, is_deleted, hunks}]``."""
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
    """Parse hunks into (added_new, removed_base) — numbers of CHANGED lines.

    Only ``+`` lines (in the new side's coordinates) and ``-`` lines (in the
    base side's coordinates); context lines are not considered changes.
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
            else:  # context
                base_line += 1
                new_line += 1
    return added, removed


def _extract_file_symbols(path: str, text: str) -> dict:
    """Re-extract a file's REAL symbols: ``{id: {lines, sig}}`` (the new side)."""
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
    """Collapse sorted line numbers into intervals ``[[a, b], ...]``."""
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
    """Check the cumulative diff against the node's boundary.

    ``original_dir`` — the worktree root (already contains the new side of the diff).
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

        # 1. A deleted symbol of a foreign node (absent from the new side).
        for sid, base in base_in_file.items():
            if sid not in new_syms and base["node"] != node["name"]:
                violations.append({
                    "symbol": sid, "path": path,
                    "reason": "foreign_symbol_in_shared_file",
                    "owner": base["node"],
                })

        # 2. A changed symbol (signature or body).
        for sid, ns in new_syms.items():
            base = base_in_file.get(sid)
            if base is None:
                continue  # new symbol — the node's own code
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

        # 3. Changed lines not attributed to an existing symbol:
        #    new symbols (no base owner) and code outside symbols.
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
