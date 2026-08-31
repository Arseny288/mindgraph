"""manifest.py — the manifest of stable nodes ``.mindvibe/nodes.yaml`` (phase 1.5).

The manifest is a git-versioned "skeleton" of semantic nodes. It pins the
identity of a node (id, title, ownership paths) so that the LLM on recomputation
does NOT rename or re-split existing nodes: symbols falling under a node's
``own_paths`` are assigned to it mechanically, and the model classifies only the
symbols not assigned to any node.

Format:

.. code-block:: yaml

    nodes:
      - id: auth
        title: Authentication
        own_paths: ["app/auth/**", "app/tokens.py"]

``own_paths`` — gitignore globs by file path relative to the repo root.
"""

from __future__ import annotations

import os

import yaml

try:
    import pathspec
except ImportError:
    pathspec = None

MANIFEST_REL = os.path.join(".mindvibe", "nodes.yaml")


def mindvibe_root(root: str) -> str:
    """The root of the MAIN repository holding ``.mindvibe/`` (not the worktree).

    Sessions run in a ``git worktree``: a worktree's ``.git`` is a FILE
    ``gitdir: <main>/.git/worktrees/<name>``. If we resolved ``.mindvibe`` from
    the working directory, every session would start with an empty manifest and
    memory. So we climb to the main repo by this pointer when ``.mindvibe`` is
    absent from the current root.
    """
    root = os.path.abspath(root)

    def _has(d):
        return os.path.isdir(os.path.join(d, ".mindvibe"))

    if _has(root):
        return root

    gitfile = os.path.join(root, ".git")
    if os.path.isfile(gitfile):
        try:
            content = open(gitfile, encoding="utf-8").read()
            if content.startswith("gitdir:"):
                gitdir = content.split(":", 1)[1].strip()
                # <main>/.git/worktrees/<name> -> <main> (three levels up)
                main_root = os.path.abspath(os.path.join(
                    gitdir, os.pardir, os.pardir, os.pardir))
                if _has(main_root):
                    return main_root
        except OSError:
            pass
    return root


def manifest_path(root: str) -> str:
    return os.path.join(mindvibe_root(root), MANIFEST_REL)


def notes_dir(root: str) -> str:
    return os.path.join(mindvibe_root(root), ".mindvibe", "notes")


def read_note(root: str, node_id: str) -> str | None:
    """Node memory ``.mindvibe/notes/<node_id>.md``; no file -> None.

    Loaded ONLY when the node is open (the agent calls it at ``open_node``).
    There is no project-level file.
    """
    path = os.path.join(notes_dir(root), f"{node_id}.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def write_note(root: str, node_id: str, content: str) -> str:
    path = os.path.join(notes_dir(root), f"{node_id}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def load(root: str) -> list:
    """Read the manifest; broken/missing -> an empty list (not fatal)."""
    p = manifest_path(root)
    if not os.path.exists(p):
        return []
    try:
        data = yaml.safe_load(open(p, encoding="utf-8").read())
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    nodes = []
    for n in data.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") is not None and n.get("title") is not None:
            nodes.append({
                "id": str(n["id"]),
                "title": str(n["title"]),
                "own_paths": [str(x) for x in (n.get("own_paths") or [])],
            })
    return nodes


def _spec(own_paths: list):
    if pathspec is None:
        return None
    return pathspec.PathSpec.from_lines("gitignore", list(own_paths))


def match_any(manifest_nodes: list, file: str) -> dict | None:
    """The first manifest node whose ``own_paths`` covers the file (or None)."""
    for n in manifest_nodes:
        spec = _spec(n["own_paths"])
        if spec is not None and spec.match_file(file):
            return n
    return None


def preassign(real_symbols: list, manifest_nodes: list) -> dict:
    """``{symbol_id: node_id}`` for symbols covered by the manifest.

    A symbol is assigned to a node by FILE (own_paths) and by STABLE id; the model
    will not see it.
    """
    pinned: dict = {}
    by_file: dict = {}
    for s in real_symbols:
        by_file.setdefault(s["file"], []).append(s["id"])
    for n in manifest_nodes:
        spec = _spec(n["own_paths"])
        if spec is None:
            continue
        for f, sids in by_file.items():
            if spec.match_file(f):
                for sid in sids:
                    pinned.setdefault(sid, n["id"])
    return pinned
