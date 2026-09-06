"""store.py — the tool's storage root (nothing lands in the analyzed repo).

Everything mindgraph writes about a project — its built graph, its
conversations, its memory — lives under an XDG-style data root OUTSIDE the
repo. ``git status`` in an analyzed repo must never show a mindgraph file.

    <root>/
      global/
        MEMORY.md              # applies to every project
      projects/
        <project_id>/
          meta.json             # abs path, display name, last_opened, remote
          graph.json             # the built graph + descriptions
          MEMORY.md              # this project only
          conversations/
            <conversation_id>.json

``project_id`` is a hash of the repo's canonical absolute path, plus its
``git remote origin`` URL when it has one. The remote lets the id survive the
directory being moved/renamed on the same machine (see :func:`resolve_project`).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field

# --- storage root (XDG, with fallbacks) --------------------------------------


def data_root() -> str:
    """The mindgraph data root for this OS.

    Linux:   $XDG_DATA_HOME/mindgraph  or  ~/.local/share/mindgraph
    macOS:   ~/Library/Application Support/mindgraph
    Windows: %LOCALAPPDATA%/mindgraph
    """
    override = os.environ.get("MINDGRAPH_DATA_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "mindgraph")


def global_dir() -> str:
    return os.path.join(data_root(), "global")


def global_memory_path() -> str:
    return os.path.join(global_dir(), "MEMORY.md")


def projects_dir() -> str:
    return os.path.join(data_root(), "projects")


# --- project identity ---------------------------------------------------------


def _git_remote_url(repo_root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    url = out.stdout.strip()
    return url or None


def project_id(repo_root: str) -> str:
    """A stable id for ``repo_root``, computed from its canonical abs path at
    the moment the project is first created. This is a create-time id only —
    once assigned it is durable and stored in ``meta.json``; a later rename
    of the directory does not change it (see :func:`resolve_project`, which
    repairs the path on a matching git remote instead of rehashing)."""
    canon = os.path.realpath(os.path.abspath(repo_root))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# --- JSON helpers --------------------------------------------------------------


def _load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --- project resolution ---------------------------------------------------------


@dataclass
class Project:
    id: str
    root: str                # <data_root>/projects/<id>
    repo_path: str            # last-known absolute path to the analyzed repo
    display_name: str
    missing: bool = False     # repo_path no longer exists on disk
    last_opened: float = field(default_factory=time.time)
    remote_url: str | None = None

    @property
    def meta_path(self) -> str:
        return os.path.join(self.root, "meta.json")

    @property
    def graph_path(self) -> str:
        return os.path.join(self.root, "graph.json")

    @property
    def memory_path(self) -> str:
        return os.path.join(self.root, "MEMORY.md")

    @property
    def conversations_dir(self) -> str:
        return os.path.join(self.root, "conversations")

    @property
    def sessions_dir(self) -> str:
        """Per-turn agent runtime state (events.jsonl, worktree/) — not part of
        the user-facing conversation model, but needs the same project root."""
        return os.path.join(self.root, "sessions")

    @property
    def layout_path(self) -> str:
        return os.path.join(self.root, "layout.json")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.root, "studio.lock")


def _meta_to_project(pid: str, meta: dict) -> Project:
    repo_path = meta.get("repo_path", "")
    missing = bool(repo_path) and not os.path.isdir(repo_path)
    return Project(
        id=pid, root=os.path.join(projects_dir(), pid), repo_path=repo_path,
        display_name=meta.get("display_name") or os.path.basename(repo_path.rstrip("/\\")) or pid,
        missing=missing, last_opened=meta.get("last_opened", 0.0),
        remote_url=meta.get("remote_url"),
    )


def _project_to_meta(p: Project) -> dict:
    return {"repo_path": p.repo_path, "display_name": p.display_name,
            "last_opened": p.last_opened, "remote_url": p.remote_url}


def _find_by_remote_at_missing_path(remote: str, exclude_pid: str | None = None) -> Project | None:
    """A project whose recorded ``repo_path`` no longer exists on disk and
    whose remote matches — the rename/move repair path. Deliberately does
    NOT match a project whose path is still live: two live checkouts of the
    same remote must stay two separate projects (never merged)."""
    for candidate in list_projects():
        if candidate.id == exclude_pid:
            continue
        if candidate.remote_url == remote and not os.path.isdir(candidate.repo_path):
            return candidate
    return None


def resolve_project(repo_root: str, *, touch: bool = True) -> Project:
    """The project for ``repo_root``, creating it on first use.

    Fast path: a project already exists at this path's id — use it directly.
    Otherwise, if the repo has a git remote, look for an existing project
    whose remote matches and whose OLD recorded path is now gone (the repo
    was moved/renamed on the same machine) and repair its path in place,
    keeping its original id — a stale id is repaired, not orphaned, and two
    still-live checkouts of the same remote are never merged.
    """
    canon = os.path.realpath(os.path.abspath(repo_root))
    remote = _git_remote_url(canon)
    pid = project_id(canon)
    root = os.path.join(projects_dir(), pid)
    meta = _load_json(os.path.join(root, "meta.json"))

    if meta is None and remote:
        repaired = _find_by_remote_at_missing_path(remote)
        if repaired is not None:
            repaired.repo_path = canon
            repaired.missing = False
            repaired.remote_url = remote
            if touch:
                repaired.last_opened = time.time()
            _write_json(repaired.meta_path, _project_to_meta(repaired))
            return repaired

    if meta is None:
        project = Project(id=pid, root=root, repo_path=canon,
                          display_name=os.path.basename(canon.rstrip("/\\")) or canon,
                          remote_url=remote)
    else:
        project = _meta_to_project(pid, meta)
        project.repo_path = canon
        project.missing = False
        project.remote_url = remote or project.remote_url
    if touch:
        project.last_opened = time.time()
        _write_json(project.meta_path, _project_to_meta(project))
    return project


def get_project(pid: str) -> Project | None:
    meta = _load_json(os.path.join(projects_dir(), pid, "meta.json"))
    if meta is None:
        return None
    return _meta_to_project(pid, meta)


def list_projects() -> list[Project]:
    pdir = projects_dir()
    if not os.path.isdir(pdir):
        return []
    out = []
    for pid in os.listdir(pdir):
        p = get_project(pid)
        if p is not None:
            out.append(p)
    out.sort(key=lambda p: p.last_opened, reverse=True)
    return out


def relocate_project(pid: str, new_path: str) -> Project | None:
    """Point an existing (possibly "missing") project at a new on-disk path —
    the explicit repair a user does from the project list, as opposed to the
    automatic repair :func:`resolve_project` does via the git remote."""
    project = get_project(pid)
    if project is None:
        return None
    project.repo_path = os.path.realpath(os.path.abspath(new_path))
    project.missing = not os.path.isdir(project.repo_path)
    _write_json(project.meta_path, _project_to_meta(project))
    return project


# --- memory (global + per-project) --------------------------------------------

MEMORY_CAP_GLOBAL_TOKENS = 1000
MEMORY_CAP_PROJECT_TOKENS = 2000


def _estimate_tokens(text: str) -> int:
    # Rough, provider-agnostic estimate (~4 chars/token in English prose) —
    # good enough to trigger a cap warning, not meant to be exact.
    return max(1, len(text) // 4)


def read_memory(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def memory_over_cap(path: str, cap_tokens: int) -> bool:
    return _estimate_tokens(read_memory(path)) > cap_tokens


def append_memory(path: str, line: str) -> str:
    """Append one durable-fact line, newline-terminated. Returns the exact
    text written (so callers can surface it verbatim, e.g. for Undo)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = line.rstrip("\n") + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return entry


def write_memory(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def clear_memory(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- conversations -------------------------------------------------------------


def _slug_title(first_message: str) -> str:
    words = re.findall(r"\S+", first_message.strip())
    title = " ".join(words[:6])
    return title or "New conversation"


def new_conversation(project: Project, first_message: str | None = None,
                     graph_fingerprint: str | None = None) -> dict:
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    conv = {
        "id": cid,
        "title": _slug_title(first_message) if first_message else "New conversation",
        "created_at": now,
        "updated_at": now,
        "graph_fingerprint": graph_fingerprint,
        "messages": [],
    }
    _write_json(os.path.join(project.conversations_dir, cid + ".json"), conv)
    return conv


def read_conversation(project: Project, cid: str) -> dict | None:
    return _load_json(os.path.join(project.conversations_dir, cid + ".json"))


def write_conversation(project: Project, conv: dict) -> None:
    """Persist the full conversation. Call after every completed turn (not
    only on exit) — a crashed process must not lose a conversation."""
    conv["updated_at"] = time.time()
    _write_json(os.path.join(project.conversations_dir, conv["id"] + ".json"), conv)


def list_conversations(project: Project) -> list[dict]:
    """Summaries (no message bodies), newest-updated first — the switcher's
    source. Use :func:`read_conversation` for the full history."""
    cdir = project.conversations_dir
    if not os.path.isdir(cdir):
        return []
    out = []
    for name in os.listdir(cdir):
        if not name.endswith(".json"):
            continue
        conv = _load_json(os.path.join(cdir, name))
        if conv is None:
            continue
        out.append({"id": conv.get("id"), "title": conv.get("title"),
                    "created_at": conv.get("created_at"),
                    "updated_at": conv.get("updated_at"),
                    "graph_fingerprint": conv.get("graph_fingerprint"),
                    "message_count": len(conv.get("messages") or [])})
    out.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return out


def rename_conversation(project: Project, cid: str, title: str) -> dict | None:
    conv = read_conversation(project, cid)
    if conv is None:
        return None
    conv["title"] = title.strip() or conv["title"]
    write_conversation(project, conv)
    return conv


def summarize_turn(meta: dict) -> str:
    """One compact line for a past turn: prompt + outcome — never the raw
    tool-call events (item 3a: raw results are what gets dropped first)."""
    prompt = (meta.get("prompt") or "").strip()
    verdict = meta.get("verdict") or {}
    state = verdict.get("state") or meta.get("state") or "?"
    touched = ", ".join(meta.get("touched_nodes") or []) or "-"
    return f'- "{prompt}" -> {state} (touched: {touched})'


def build_turn_context(project: Project, prior_turns: list) -> str:
    """What a new turn's agent process sees beyond the task itself: durable
    memory (global, then project) plus a compacted summary of this
    conversation's earlier turns. Raw tool-call results never appear here —
    only prompts and outcomes (item 3a); each turn is otherwise a fresh
    process with no memory of earlier ones at all."""
    parts = []
    g = read_memory(global_memory_path()).strip()
    if g:
        parts.append("# Global memory (applies to every project)\n" + g)
    p = read_memory(project.memory_path).strip()
    if p:
        parts.append("# Project memory (this codebase)\n" + p)
    if prior_turns:
        lines = "\n".join(summarize_turn(t) for t in prior_turns)
        parts.append("# Earlier turns in this conversation\n" + lines)
    return "\n\n".join(parts)


def delete_conversation(project: Project, cid: str) -> bool:
    path = os.path.join(project.conversations_dir, cid + ".json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# --- data sizes / destructive actions (settings "Data" section) ---------------


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def data_summary(project: Project) -> dict:
    """Sizes for the settings "Data" section — real bytes on disk, not
    estimates, so every destructive action can say exactly what disappears."""
    convs = list_conversations(project)
    return {
        "project_memory_bytes": _file_size(project.memory_path),
        "global_memory_bytes": _file_size(global_memory_path()),
        "conversations_count": len(convs),
        "conversations_bytes": _dir_size(project.conversations_dir),
        "graph_bytes": _file_size(project.graph_path),
        "graph_built_at": (os.path.getmtime(project.graph_path)
                          if os.path.exists(project.graph_path) else None),
        "project_bytes": _dir_size(project.root),
        "storage_root": data_root(),
    }


def clear_project_memory(project: Project) -> None:
    clear_memory(project.memory_path)


def clear_global_memory() -> None:
    clear_memory(global_memory_path())


def delete_all_conversations(project: Project) -> None:
    shutil.rmtree(project.conversations_dir, ignore_errors=True)


def delete_graph(project: Project) -> None:
    try:
        os.remove(project.graph_path)
    except OSError:
        pass


def delete_project(pid: str) -> None:
    """Delete everything for one project. Memory, conversations and graph all
    go — this is "This project → Delete everything for this project", not the
    narrower per-row deletes above."""
    shutil.rmtree(os.path.join(projects_dir(), pid), ignore_errors=True)


def delete_all_data() -> None:
    """Delete the entire storage root — "All mindgraph data". The app returns
    to first-run state; nothing needs a restart since every read here is
    read-through (see resolve_project/list_projects re-creating on demand)."""
    shutil.rmtree(data_root(), ignore_errors=True)


# --- export / import ------------------------------------------------------------


def export_project(project: Project, out_dir: str) -> str:
    """Write graph.json, MEMORY.md and conversations/ into ``out_dir`` — the
    explicit, user-triggered answer to "data doesn't travel with the repo"."""
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(project.graph_path):
        shutil.copy2(project.graph_path, os.path.join(out_dir, "graph.json"))
    if os.path.exists(project.memory_path):
        shutil.copy2(project.memory_path, os.path.join(out_dir, "MEMORY.md"))
    if os.path.isdir(project.conversations_dir):
        dst = os.path.join(out_dir, "conversations")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(project.conversations_dir, dst)
    _write_json(os.path.join(out_dir, "export_meta.json"),
               {"project_id": project.id, "repo_path": project.repo_path,
                "display_name": project.display_name, "exported_at": time.time()})
    return out_dir


def import_project(repo_root: str, in_dir: str) -> Project:
    """The inverse of :func:`export_project`: materializes ``in_dir``'s
    contents into the project resolved for ``repo_root`` (creating it if
    needed). Existing files at the destination are overwritten."""
    project = resolve_project(repo_root)
    graph_src = os.path.join(in_dir, "graph.json")
    if os.path.exists(graph_src):
        os.makedirs(project.root, exist_ok=True)
        shutil.copy2(graph_src, project.graph_path)
    mem_src = os.path.join(in_dir, "MEMORY.md")
    if os.path.exists(mem_src):
        shutil.copy2(mem_src, project.memory_path)
    conv_src = os.path.join(in_dir, "conversations")
    if os.path.isdir(conv_src):
        shutil.rmtree(project.conversations_dir, ignore_errors=True)
        shutil.copytree(conv_src, project.conversations_dir)
    return project
