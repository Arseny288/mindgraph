"""studio.py — the local agent studio (canvas as a live surface).

One process, four roles (see STUDIO.md §2):
  1. serves the viewer static;
  2. REST + SSE for the canvas;
  3. supervisor of the TS agent child process (spawn/stdin/stdout/kill);
  4. repository file watcher — later.

The source of truth is FILES, not process memory: everything lives under
``<repo>/.mindvibe/studio/``:

    layout.json            # node positions moved by hand
    studio.lock            # pid + port (second-instance guard)
    sessions/<id>/
        meta.json          # state, scope, prompt, plan, verdict
        events.jsonl       # what the agent writes (format already exists)
        worktree/          # the execution branch

The studio is a read-through view over these files. If the process is killed and
restarted, it resumes the last unfinished session from ``meta.json``.

Every API response carries ``graph_version`` (hash of ``graph.json``): if the
client sees a foreign version it reloads the graph instead of painting over the
old one (STUDIO.md §4).
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
import webbrowser

from .serve import viewer_dir

# Session states (STUDIO.md §3).
STATE_IDLE = "idle"
STATE_PLANNING = "planning"
STATE_PLAN_READY = "plan_ready"
STATE_EXECUTING = "executing"
STATE_REVIEW = "review"
STATE_APPLIED = "applied"
STATE_FAILED = "failed"
STATE_ABORTED = "aborted"

_TERMINAL = {STATE_REVIEW, STATE_APPLIED, STATE_FAILED, STATE_ABORTED}

_STUDIO_DIRNAME = "studio"
_MAX_PLAN_NODES = 3  # STUDIO.md §7: block Run if the plan touches more than 3 nodes.


def studio_dir(repo_root: str) -> str:
    return os.path.join(os.path.abspath(repo_root), ".mindvibe", _STUDIO_DIRNAME)


def graph_path(repo_root: str) -> str:
    return os.path.join(os.path.abspath(repo_root), ".mindgraph", "graph.json")


def graph_version(repo_root: str) -> str:
    """sha256 of graph.json — the version token the client checks on every call."""
    try:
        with open(graph_path(repo_root), "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return "missing"


def _load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


# --- layout (layout.json: view settings + hand-moved node positions) ---------

DEFAULT_LAYOUT = {"view": {"rail_width": 420, "rail_collapsed": False},
                  "positions": {}}


def layout_path(repo_root: str) -> str:
    return os.path.join(studio_dir(repo_root), "layout.json")


def read_layout(repo_root: str) -> dict:
    data = _load_json(layout_path(repo_root), None)
    if data is None:
        return {"view": dict(DEFAULT_LAYOUT["view"]), "positions": {}}
    # tolerate the older flat shape: {mode, panel_width, panel_collapsed, positions}
    if "view" not in data:
        data = {"view": {"rail_width": data.get("panel_width", 420),
                         "rail_collapsed": data.get("panel_collapsed", False)},
                "positions": data.get("positions", {})}
    return data


def _valid_node_ids(repo_root: str) -> set:
    g = _load_json(graph_path(repo_root)) or {}
    return ({n["id"] for n in g.get("nodes", [])}
            | {d["id"] for d in g.get("domains", [])}
            | {s["id"] for s in g.get("symbols", [])})


def name_area(repo_root: str, title: str, files: list) -> str:
    """Name an "Unassigned" area (STUDIO.md §10): pin its files in
    ``.mindvibe/nodes.yaml`` as a new node; the next recompute assigns them
    mechanically. Returns the manifest node id."""
    from . import manifest
    taken = {n["id"] for n in manifest.load(repo_root)} | _valid_node_ids(repo_root)
    base = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", title.strip().lower()).strip("_") or "node"
    nid = base
    k = 2
    while nid in taken:
        nid = f"{base}_{k}"
        k += 1
    manifest.add_node(repo_root, {"id": nid, "title": title.strip(),
                                  "own_paths": sorted(set(files))})
    return nid


def write_layout(repo_root: str, data: dict) -> dict:
    """Merge a partial update into layout.json. ``view`` keys merge into the view
    branch; ``positions`` keys merge into the positions branch (``positions: null``
    clears it). Stale node ids are dropped on write."""
    cur = read_layout(repo_root)
    if "view" in data and isinstance(data["view"], dict):
        cur["view"] = {**cur.get("view", {}), **data["view"]}
    if "positions" in data:
        p = data["positions"]
        if p is None:
            cur["positions"] = {}
        elif isinstance(p, dict):
            cur["positions"] = {**cur.get("positions", {}), **p}
    valid = _valid_node_ids(repo_root)
    cur["positions"] = {k: v for k, v in cur["positions"].items() if k in valid}
    _write_json(layout_path(repo_root), cur)
    return cur


# --- agent runtime -----------------------------------------------------------

def agent_bundle() -> str:
    """The bundled agent.cjs, or the TS source in a dev checkout."""
    bundle = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_agent", "agent.cjs")
    if os.path.exists(bundle):
        return bundle
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "agent", "src", "main.ts")


def _node_bin() -> str | None:
    return shutil.which("node")


# Running agent processes, keyed by session id — for /api/stop.
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()


def _mindgraph_bin() -> str:
    return shutil.which("mindgraph") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "bin", "mindgraph")


# --- session store -----------------------------------------------------------

def _sessions_dir(repo_root: str) -> str:
    return os.path.join(studio_dir(repo_root), "sessions")


def create_session(repo_root: str, scope_node_id: str | None, prompt: str) -> str:
    sid = uuid.uuid4().hex[:12]
    meta = {
        "id": sid,
        "created_at": time.time(),
        "state": STATE_IDLE,
        "scope_node_id": scope_node_id,
        "prompt": prompt,
        "plan": None,
        "verdict": None,
    }
    _write_json(os.path.join(_sessions_dir(repo_root), sid, "meta.json"), meta)
    return sid


def read_session(repo_root: str, session_id: str) -> dict | None:
    return _load_json(os.path.join(_sessions_dir(repo_root), session_id, "meta.json"))


def write_session(repo_root: str, session_id: str, **fields) -> dict:
    meta = read_session(repo_root, session_id) or {"id": session_id}
    meta.update(fields)
    _write_json(os.path.join(_sessions_dir(repo_root), session_id, "meta.json"), meta)
    return meta


def _append_event(repo_root: str, session_id: str, event: dict) -> None:
    path = os.path.join(_sessions_dir(repo_root), session_id, "events.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    event = {**event, "ts": time.time()}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_state(repo_root: str) -> dict:
    """The current session state (STUDIO.md §3): the latest session (terminal or
    not) — the canvas still needs review to apply/discard. Idle only when there
    are no sessions at all."""
    sessions_dir = _sessions_dir(repo_root)
    if not os.path.isdir(sessions_dir):
        return {"state": STATE_IDLE, "session_id": None}
    sessions = os.listdir(sessions_dir)
    if not sessions:
        return {"state": STATE_IDLE, "session_id": None}
    # Most recently created session wins.
    def _created(sid: str) -> float:
        meta = _load_json(os.path.join(sessions_dir, sid, "meta.json"))
        return (meta or {}).get("created_at", 0.0)
    sid = sorted(sessions, key=_created)[-1]
    meta = _load_json(os.path.join(sessions_dir, sid, "meta.json"))
    if meta is None:
        return {"state": STATE_IDLE, "session_id": None}
    return {"state": meta.get("state"), "session_id": sid, **meta}


def _session_events(repo_root: str, session_id: str | None) -> list:
    if not session_id:
        return []
    path = os.path.join(_sessions_dir(repo_root), session_id, "events.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def list_sessions(repo_root: str) -> list:
    """All sessions (turns), newest first, each with its events — the rail's source."""
    sdir = _sessions_dir(repo_root)
    if not os.path.isdir(sdir):
        return []
    out = []
    for sid in os.listdir(sdir):
        meta = _load_json(os.path.join(sdir, sid, "meta.json"))
        if meta is None:
            continue
        meta["events"] = _session_events(repo_root, sid)
        out.append(meta)
    out.sort(key=lambda m: m.get("created_at", 0))
    return out


def _graph(repo_root: str) -> dict:
    return _load_json(graph_path(repo_root)) or {}


def _node_names_by_id(repo_root: str) -> dict:
    return {n["id"]: n["name"] for n in _graph(repo_root).get("nodes", [])}


def _node_owners_by_file(repo_root: str) -> dict:
    owners = {}
    for n in _graph(repo_root).get("nodes", []):
        for f in n.get("files", []):
            owners[f] = n["name"]
    return owners


def _touched_from_files(repo_root: str, files: list) -> list:
    owners = _node_owners_by_file(repo_root)
    return sorted({owners.get(f, "?") for f in files if owners.get(f)})


_RECOMPUTE_LOCK = threading.Lock()  # serialize rebuild/watch/apply recomputes


def run_recompute(repo_root: str, timeout: int = 300) -> str:
    """Recompute graph.json in-process (``--no-llm --force`` semantics: statics
    are re-extracted, the LLM layout is preserved, new symbols land in the
    manifest or "Unassigned" — see emit.build_graph). Raises RuntimeError."""
    started = time.time()
    with _RECOMPUTE_LOCK:
        try:
            from . import config as _cfg
            from . import emit as _emit
            from . import llmcache as _lc
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"recompute imports failed: {e}") from e
        try:
            cfg = _cfg.resolve_config(repo_root)
            cache_base = _lc.resolve_base(cfg.cache_dir)
            _emit.load_or_build(
                repo_root, provider_name=cfg.provider, no_llm=True, force=True,
                regroup=False, extra_ignore=cfg.ignore, model=cfg.model,
                cache_base=cache_base, entry_points=cfg.entry_points,
                node_min=cfg.node_min, node_max=cfg.node_max)
        except Exception as e:
            raise RuntimeError(f"recompute failed: {e}") from e
    return f"ok ({time.time() - started:.1f}s)"


def recompute_graph(repo_root: str) -> None:
    """Recompute after apply (fire-and-forget; errors are swallowed — the next
    explicit Rebuild or the watcher will surface them)."""
    try:
        run_recompute(repo_root)
    except Exception:
        pass


# --- file watcher (STUDIO.md §10 / §14 step 5) -------------------------------

_WATCH_PRUNE = {".git", ".mindgraph", ".mindvibe", ".venv", "venv", "node_modules",
                "__pycache__", ".idea", ".pytest_cache", "build", "dist", ".hg"}


def _py_snapshot(repo_root: str) -> dict:
    """{relative path: (mtime_ns, size)} for every .py file under the repo
    (cheap stat snapshot — no content hashing)."""
    out: dict = {}
    base = os.path.abspath(repo_root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if d not in _WATCH_PRUNE and not d.endswith(".egg-info")]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rel = os.path.relpath(p, base)
            out[rel] = (st.st_mtime_ns, st.st_size)
    return out


def _watch_active(repo_root: str) -> bool:
    """Don't rebuild the graph under a running agent session — the worktree it
    edits is separate, but a mid-run recompute would move the map under the
    user's feet while events stream."""
    try:
        st = read_state(repo_root).get("state")
    except Exception:
        return True
    return st in (STATE_PLANNING, STATE_EXECUTING)


def start_watcher(repo_root: str, interval: float = 0.5,
                  debounce: float = 0.7) -> None:
    """Poll the repo for .py changes and recompute graph.json when the tree is
    quiet. Daemon; errors never kill the server."""
    snapshot = _py_snapshot(repo_root)
    pending = False

    def _loop():
        nonlocal snapshot, pending
        while True:
            time.sleep(interval)
            try:
                snap = _py_snapshot(repo_root)
            except Exception:
                continue
            busy = _watch_active(repo_root)
            if snap != snapshot:
                if busy:
                    snapshot = snap
                    pending = True
                    continue
                # Debounce: wait until the tree stops changing.
                last_change = time.time()
                last = snap
                while time.time() - last_change < debounce:
                    time.sleep(0.25)
                    try:
                        cur = _py_snapshot(repo_root)
                    except Exception:
                        cur = last
                    if cur != last:
                        last_change = time.time()
                        last = cur
                snapshot = last
                pending = False
                try:
                    run_recompute(repo_root)
                except Exception:
                    pass
            elif pending and not busy:
                pending = False
                try:
                    run_recompute(repo_root)
                except Exception:
                    pass

    threading.Thread(target=_loop, daemon=True,
                     name="mindgraph-studio-watcher").start()


# --- worktree ----------------------------------------------------------------

def _worktree_dir(repo_root: str, session_id: str) -> str:
    return os.path.join(_sessions_dir(repo_root), session_id, "worktree")


def make_worktree(repo_root: str, session_id: str) -> str:
    wt = _worktree_dir(repo_root, session_id)
    shutil.rmtree(wt, ignore_errors=True)
    os.makedirs(wt, exist_ok=True)
    for name in os.listdir(repo_root):
        if name in (".mindgraph", ".mindvibe") or name.startswith(".git"):
            continue
        src = os.path.join(repo_root, name)
        dst = os.path.join(wt, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
        else:
            shutil.copy2(src, dst)
    subprocess.run(["git", "init", "-q", wt], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.email", "studio@local"], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.name", "studio"], check=True)
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-q", "-m", "base"], check=True)
    return wt


def _changed_files(repo_root: str, session_id: str) -> list:
    """The worktree paths that differ from the session's base commit (in any
    way: modified, added, deleted)."""
    wt = _worktree_dir(repo_root, session_id)
    if not os.path.isdir(os.path.join(wt, ".git")):
        return []
    try:
        out = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return []
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        code, _, path = line[:2], line[2], line[3:]
        # Renames come as "R  old -> new" in newer git; keep the target only.
        if path.startswith('"'):
            path = path  # quoted path with special chars — reported as-is
        if code[0] == "R":
            path = path.split(" -> ")[-1]
        if path:
            paths.append(path)
    return paths


def _file_patch(repo_root: str, session_id: str, rel: str, untracked: bool) -> dict:
    """A unified-ish diff for one worktree path (STUDIO.md §9).

    Returns ``{"path", "status", "diff"}`` where ``diff`` is the unified text
    for tracked changes and a ``+``-prefixed add-patch for untracked files
    (``git diff`` does not see untracked files).
    """
    wt = _worktree_dir(repo_root, session_id)
    src = os.path.join(wt, rel)
    if os.path.exists(src) and not untracked:
        try:
            out = subprocess.run(
                ["git", "-C", wt, "diff", "--no-ext-diff", "--", rel],
                capture_output=True, text=True, check=True, timeout=30).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            out = ""
        if out.strip():
            return {"path": rel, "status": "modified", "diff": out}
    # Untracked new file (or a tracked change git could not diff): show the
    # whole content as an add-patch so Apply is reviewable before it lands.
    if os.path.exists(src):
        try:
            with open(src, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        body = "\n".join("+" + ln for ln in lines)
        if lines:
            body += "\n"
        head = (f"diff --git a/{rel} b/{rel}\n"
                f"new file mode 100644\n"
                f"--- /dev/null\n"
                f"+++ b/{rel}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n")
        return {"path": rel, "status": "new", "diff": head + body}
    # Deleted from the worktree.
    try:
        out = subprocess.run(
            ["git", "-C", wt, "diff", "--no-ext-diff", "--", rel],
            capture_output=True, text=True, check=True, timeout=30).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        out = ""
    return {"path": rel, "status": "deleted", "diff": out or f"deleted: {rel}\n"}


def session_diff(repo_root: str, session_id: str) -> dict:
    """The session's review diff, grouped by owning node (STUDIO.md §9: the
    user thinks in zones, not file paths). Returns ``{"changed", "groups"}``.

    ``groups`` — ``[{"node": <name>, "files": [{"path", "status", "diff"}]}]``.
    Files unknown to the current graph (e.g. newly created ones) group under
    ``Other`` until the next recompute owns them.
    """
    wt = _worktree_dir(repo_root, session_id)
    if not os.path.isdir(os.path.join(wt, ".git")):
        return {"changed": [], "groups": []}
    changed = _changed_files(repo_root, session_id)
    untracked = set()
    try:
        out = subprocess.run(
            ["git", "-C", wt, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True, timeout=30).stdout
        untracked = set(p for p in out.splitlines() if p)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    owners = _node_owners_by_file(repo_root)
    groups: dict = {}
    order: list = []
    for rel in changed:
        node = owners.get(rel)
        if node is None:
            # A changed file that the (stale) graph does not own yet.
            node = "Other"
        if node not in groups:
            groups[node] = []
            order.append(node)
        groups[node].append(_file_patch(repo_root, session_id, rel, rel in untracked))
    return {"changed": changed,
            "groups": [{"node": n, "files": groups[n]} for n in order]}


def apply_session(repo_root: str, session_id: str) -> list:
    """Copy the worktree's changed files into the working tree. Returns changed paths."""
    wt = _worktree_dir(repo_root, session_id)
    changed = _changed_files(repo_root, session_id)
    for rel in changed:
        src = os.path.join(wt, rel)
        dst = os.path.join(repo_root, rel)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    return changed


def discard_session(repo_root: str, session_id: str) -> None:
    shutil.rmtree(_worktree_dir(repo_root, session_id), ignore_errors=True)
    write_session(repo_root, session_id, state=STATE_IDLE)


# --- agent supervisor --------------------------------------------------------

def _run_agent(repo_root: str, session_id: str, *, plan: bool,
               scope_node_id: str | None, prompt: str) -> None:
    """Spawn the agent, stream its JSONL events to events.jsonl, update meta.json."""
    node = _node_bin()
    if node is None:
        write_session(repo_root, session_id, state=STATE_FAILED,
                      error="node not found")
        return

    cmd = [node, agent_bundle(), "--node", scope_node_id or "", "--task", prompt,
           "--dir", repo_root, "--bin", _mindgraph_bin(), "--json",
           "--max-moves", "40", "--max-refusals", "6"]
    # Config overrides (mindgraph.toml / MINDGRAPH_*): the agent's own defaults
    # stay untouched when nothing is configured.
    try:
        from . import config as _cfg
        resolved = _cfg.resolve_config(repo_root)
        if resolved.provider != "deepseek":
            cmd += ["--provider", resolved.provider]
        if resolved.model:
            cmd += ["--model", resolved.model]
    except Exception:
        pass
    if plan:
        cmd.append("--plan")
        cmd += ["--worktree", repo_root]
    else:
        wt = make_worktree(repo_root, session_id)
        cmd += ["--worktree", wt]

    _append_event(repo_root, session_id, {"type": "session.started",
                                          "scope_node_id": scope_node_id})
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=dict(os.environ))
    with _RUNNING_LOCK:
        _RUNNING[session_id] = proc
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            _append_event(repo_root, session_id, ev)
            if ev.get("type") == "plan.ready":
                plan_data = {k: ev.get(k) for k in ("nodes", "files", "contracts",
                                                    "tests", "summary")}
                blocked = len(plan_data.get("nodes") or []) > _MAX_PLAN_NODES
                names = _node_names_by_id(repo_root)
                touched = [names.get(i, i) for i in (plan_data.get("nodes") or [])]
                write_session(repo_root, session_id, plan=plan_data,
                              plan_blocked=blocked,
                              touched_nodes=touched,
                              state=STATE_IDLE if blocked else STATE_PLAN_READY)
            elif ev.get("type") == "verify_verdict" and ev.get("ok") is False:
                write_session(repo_root, session_id, verdict={
                    "state": "failed", "violations": ev.get("violations", []),
                })
        proc.wait()
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(session_id, None)

    stderr = proc.stderr.read() if proc.stderr else ""
    meta = read_session(repo_root, session_id) or {}
    if plan:
        # If the model never called submit_plan, the plan is empty — not ready.
        if meta.get("plan") is None and meta.get("state") != STATE_IDLE:
            write_session(repo_root, session_id, state=STATE_FAILED,
                          error="no plan produced")
    else:
        verdict = meta.get("verdict")
        if meta.get("aborted"):
            pass  # stop_session already wrote the aborted verdict
        else:
            if verdict is None or verdict.get("state") != "failed":
                graph = _load_json(graph_path(repo_root)) or {}
                incomplete = bool((graph.get("stats", {}).get("external") or []))
                changed = _changed_files(repo_root, session_id)
                verdict = {"state": "ok" if proc.returncode == 0 else "failed",
                           "changed": changed,
                           "complete": not incomplete}
                write_session(repo_root, session_id, state=STATE_REVIEW, verdict=verdict,
                              touched_nodes=_touched_from_files(repo_root, changed))
    _append_event(repo_root, session_id, {"type": "session.finished",
                                          "returncode": proc.returncode})


def stop_session(repo_root: str, session_id: str) -> None:
    """Kill the running agent; the worktree remains for inspection (§3)."""
    with _RUNNING_LOCK:
        proc = _RUNNING.get(session_id)
    if proc is not None and proc.poll() is None:
        proc.kill()
    write_session(repo_root, session_id, aborted=True,
                  state=STATE_REVIEW,
                  verdict={"state": "aborted",
                           "changed": _changed_files(repo_root, session_id)})


def start_plan(repo_root: str, session_id: str) -> None:
    meta = read_session(repo_root, session_id)
    write_session(repo_root, session_id, state=STATE_PLANNING)
    threading.Thread(target=_run_agent, daemon=True, kwargs={
        "repo_root": repo_root, "session_id": session_id, "plan": True,
        "scope_node_id": meta.get("scope_node_id"), "prompt": meta.get("prompt"),
    }).start()


def start_execute(repo_root: str, session_id: str, scope_node_id: str | None = None) -> None:
    meta = read_session(repo_root, session_id)
    if meta.get("plan_blocked"):
        return
    node = scope_node_id or meta.get("scope_node_id")
    if not node:
        nodes = (meta.get("plan") or {}).get("nodes") or []
        if len(nodes) == 1:
            node = nodes[0]
        else:
            write_session(repo_root, session_id, error="pick an area to run")
            return
    write_session(repo_root, session_id, scope_node_id=node, state=STATE_EXECUTING)
    threading.Thread(target=_run_agent, daemon=True, kwargs={
        "repo_root": repo_root, "session_id": session_id, "plan": False,
        "scope_node_id": node, "prompt": meta.get("prompt"),
    }).start()


# --- HTTP --------------------------------------------------------------------

def _make_handler(repo_root: str):
    graph_file = graph_path(repo_root)

    # Effective provider/model (mindgraph.toml / MINDGRAPH_* / defaults) — the
    # honest label under the composer mode (Plan · deepseek-v4-flash).
    try:
        from . import config as _cfg
        _cfg_resolved = _cfg.resolve_config(repo_root)
    except Exception:
        _cfg_resolved = None

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".jsx": "text/javascript"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=viewer_dir(), **kw)

        def _send_json(self, obj: dict, status: int = 200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _query(self) -> dict:
            """Decoded query params (ids are often non-ASCII — without
            unquoting, a query like ?id=… 404s on Cyrillic ids)."""
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            if not qs:
                return {}
            try:
                from urllib.parse import parse_qs
                return {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}
            except Exception:
                return {}

        # --- GET ----------------------------------------------------------
        def api_graph(self):
            graph = _load_json(graph_file)
            if graph is None:
                self._send_json({"error": "no graph.json"}, status=404)
                return
            self._send_json({"graph_version": graph_version(repo_root), "graph": graph})

        def api_state(self):
            agent = None
            if _cfg_resolved is not None:
                agent = {"provider": _cfg_resolved.provider,
                         "model": _cfg_resolved.model or "deepseek-v4-flash"}
            self._send_json({"graph_version": graph_version(repo_root),
                             "state": read_state(repo_root),
                             "agent": agent})

        def api_sessions(self):
            self._send_json({"sessions": list_sessions(repo_root)})

        def api_events(self):
            # SSE: replay the session's full history, then tail new lines.
            params = self._query()
            sid = params.get("session_id") or read_state(repo_root).get("session_id")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            path = os.path.join(_sessions_dir(repo_root), sid, "events.jsonl") if sid else ""
            sent = 0
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                            sent += 1
            except OSError:
                pass
            try:
                while True:
                    try:
                        with open(path, encoding="utf-8") as fh:
                            lines = fh.readlines()
                    except OSError:
                        lines = []
                    for line in lines[sent:]:
                        line = line.strip()
                        if line:
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        sent += 1
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def api_diff(self):
            params = self._query()
            sid = params.get("session_id")
            if not sid:
                self._send_json({"error": "session_id required"}, status=400)
                return
            # §9: Apply is only meaningful after the user actually saw the diff —
            # record the fetch so the review is honest about what was shown.
            write_session(repo_root, sid, diff_seen=True)
            self._send_json(session_diff(repo_root, sid))

        def api_layout_get(self):
            self._send_json({"layout": read_layout(repo_root)})

        def api_layout_post(self):
            body = self._read_body()
            layout = write_layout(repo_root, body)
            self._send_json({"layout": layout})

        # --- POST ---------------------------------------------------------
        def api_plan(self):
            body = self._read_body()
            sid = create_session(repo_root, body.get("scope_node_id"),
                                 body.get("prompt") or "")
            start_plan(repo_root, sid)
            self._send_json({"session_id": sid, "state": read_state(repo_root)})

        def api_run(self):
            body = self._read_body()
            sid = body.get("session_id")
            if not sid or read_session(repo_root, sid) is None:
                self._send_json({"error": "unknown session"}, status=400)
                return
            start_execute(repo_root, sid, body.get("scope_node_id"))
            self._send_json({"session_id": sid, "state": read_state(repo_root)})

        def api_apply(self):
            body = self._read_body()
            sid = body.get("session_id")
            changed = apply_session(repo_root, sid) if sid else []
            if sid:
                write_session(repo_root, sid, state=STATE_APPLIED,
                              applied_changed=changed)
                threading.Thread(target=recompute_graph, args=(repo_root,),
                                 daemon=True).start()
            self._send_json({"changed": changed})

        def api_stop(self):
            body = self._read_body()
            sid = body.get("session_id")
            if sid:
                stop_session(repo_root, sid)
            self._send_json({"ok": True, "state": read_state(repo_root)})

        def api_rebuild(self):
            # Full recompute on demand (STUDIO.md §10 "Rebuild" button). Runs in
            # this handler thread (the TCPServer threads requests), so the canvas
            # keeps serving while the scan is in flight.
            try:
                run_recompute(repo_root)
                self._send_json({"ok": True,
                                 "graph_version": graph_version(repo_root)})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        def api_nodes(self):
            # Name an "Unassigned" area (STUDIO.md §10): pins the area's files
            # as a manifest node and recomputes so the assignment is mechanical.
            body = self._read_body()
            title = (body.get("title") or "").strip()
            sids = body.get("symbol_ids")
            graph = _load_json(graph_file) or {}
            if not title:
                self._send_json({"error": "title required"}, status=400)
                return
            if not isinstance(sids, list) or not sids:
                self._send_json({"error": "symbol_ids required"}, status=400)
                return
            by_id = {s["id"]: s for s in graph.get("symbols", [])}
            files = sorted({by_id[sid]["file"] for sid in sids if sid in by_id})
            if not files:
                self._send_json({"error": "no known symbols for these ids"},
                                status=400)
                return
            nid = name_area(repo_root, title, files)
            try:
                run_recompute(repo_root)
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e), "node_id": nid},
                                status=500)
                return
            self._send_json({"ok": True, "node_id": nid, "title": title,
                             "graph_version": graph_version(repo_root),
                             "files": files})

        def api_discard(self):
            body = self._read_body()
            sid = body.get("session_id")
            if sid:
                discard_session(repo_root, sid)
                # Discarding a pending plan removes it: the composer goes back
                # to accepting a new message instead of staying blocked.
                write_session(repo_root, sid, plan=None, plan_blocked=False)
            self._send_json({"ok": True, "state": read_state(repo_root)})

        # --- routing ------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/graph":
                self.api_graph()
            elif path == "/api/state":
                self.api_state()
            elif path == "/api/events":
                self.api_events()
            elif path == "/api/diff":
                self.api_diff()
            elif path == "/api/layout":
                self.api_layout_get()
            elif path == "/api/sessions":
                self.api_sessions()
            else:
                super().do_GET()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/plan":
                self.api_plan()
            elif path == "/api/run":
                self.api_run()
            elif path == "/api/apply":
                self.api_apply()
            elif path == "/api/stop":
                self.api_stop()
            elif path == "/api/discard":
                self.api_discard()
            elif path == "/api/rebuild":
                self.api_rebuild()
            elif path == "/api/nodes":
                self.api_nodes()
            elif path == "/api/layout":
                self.api_layout_post()
            else:
                self._send_json({"error": "not found"}, status=404)

        def translate_path(self, path):
            if path.split("?", 1)[0] == "/graph.json":
                return graph_file
            return super().translate_path(path)

        def log_message(self, fmt, *args):
            pass

    return Handler


class _StudioTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(repo_root: str, port: int = 5173):
    repo_root = os.path.abspath(repo_root)
    if not os.path.exists(graph_path(repo_root)):
        raise FileNotFoundError(
            f"{graph_path(repo_root)} not found — run `mindgraph scan <repo>` first")
    return _StudioTCPServer(("127.0.0.1", port), _make_handler(repo_root))


def run(repo_root: str, port: int = 5173, open_browser: bool = True,
        watch: bool = True) -> None:
    httpd = make_server(repo_root, port=port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"studio: {url}  (Ctrl+C to stop)")

    if watch:
        # File watcher (STUDIO.md §10): rebuild graph.json when the code
        # changes and the tree is quiet; the client sees a new graph_version.
        try:
            start_watcher(repo_root)
        except Exception:
            pass

    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
