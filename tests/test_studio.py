"""Studio server tests (studio.py, STUDIO.md §14 step 1).

The studio serves the viewer static AND the REST/SSE API for the canvas, with
every response carrying ``graph_version`` so the client can detect staleness.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request

import pytest

from mindgraph import studio


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path_factory, monkeypatch):
    # Storage amendment: graph.json/sessions/layout now live in the tool's own
    # XDG-style storage root (store.py), never inside the analyzed repo —
    # isolate it per test so tests don't collide with (or pollute) the real
    # user data dir.
    monkeypatch.setenv("MINDGRAPH_DATA_HOME", str(tmp_path_factory.mktemp("data")))


@pytest.fixture()
def repo(tmp_path):
    studio._write_json(studio.graph_path(str(tmp_path)),
                       {"version": 1, "repo": {"name": "test"},
                        "nodes": [{"id": "n_auth", "name": "auth", "files": []}],
                        "domains": [], "symbols": [], "node_edges": [],
                        "stats": {"external": []}})
    return str(tmp_path)


@pytest.fixture()
def server(repo):
    httpd = studio.make_server(repo, port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read()


def _get_headers(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, dict(r.headers)


def test_api_graph_returns_graph_and_version(server):
    status, body = _get(server, "/api/graph")
    assert status == 200
    data = json.loads(body)
    assert data["graph"]["repo"]["name"] == "test"
    assert data["graph_version"].startswith("sha256:")


def test_api_state_idle_when_no_session(server):
    status, body = _get(server, "/api/state")
    assert status == 200
    data = json.loads(body)
    assert data["state"]["state"] == studio.STATE_IDLE
    assert data["state"]["session_id"] is None
    assert data["graph_version"].startswith("sha256:")


def test_api_events_is_sse(server):
    # The SSE endpoint keeps the connection open (it tails events.jsonl), so we
    # check the status + Content-Type header and close without reading the body.
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    conn.close()


def test_studio_static_served(server):
    status, body = _get(server, "/")
    assert status == 200
    assert re.search(rb"/dist/app\.[0-9a-f]{10}\.js", body)


# --- caching hardening pass: studio.py shares serve.py's ONE cache policy ---

def _hashed_js_path(body: bytes) -> str:
    m = re.search(rb"/dist/(app\.[0-9a-f]{10}\.js)", body)
    assert m, "no hashed app.<hash>.js reference in index.html"
    return "/dist/" + m.group(1).decode()


def test_studio_index_html_is_no_store(server):
    status, headers = _get_headers(server, "/")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_studio_hashed_asset_is_immutable(server):
    _, body = _get(server, "/")
    status, headers = _get_headers(server, _hashed_js_path(body))
    assert status == 200
    assert headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_studio_api_response_is_no_store(server):
    status, headers = _get_headers(server, "/api/state")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_studio_graph_json_is_no_store(server):
    status, headers = _get_headers(server, "/graph.json")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_studio_dev_mode_disables_hashed_asset_caching(repo):
    httpd = studio.make_server(repo, port=0, dev=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        _, body = _get(port, "/")
        status, headers = _get_headers(port, _hashed_js_path(body))
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)


def test_state_reflects_open_session(repo):
    # An open (non-terminal) session surfaces in /api/state.
    sessions = os.path.join(studio.studio_dir(repo), "sessions", "s1")
    os.makedirs(sessions)
    with open(os.path.join(sessions, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"state": studio.STATE_EXECUTING, "prompt": "add docstring"}, fh)
    state = studio.read_state(repo)
    assert state["state"] == studio.STATE_EXECUTING
    assert state["session_id"] == "s1"
    assert state["prompt"] == "add docstring"


# --- session store ----------------------------------------------------------

def test_session_store_roundtrip(repo):
    sid = studio.create_session(repo, "auth", "do a thing")
    assert sid
    meta = studio.read_session(repo, sid)
    assert meta["state"] == studio.STATE_IDLE
    assert meta["scope_node_id"] == "auth"
    assert meta["prompt"] == "do a thing"

    studio.write_session(repo, sid, state=studio.STATE_PLAN_READY,
                         plan={"nodes": ["auth"], "files": ["auth/login.py"]})
    meta = studio.read_session(repo, sid)
    assert meta["state"] == studio.STATE_PLAN_READY
    assert meta["plan"]["files"] == ["auth/login.py"]


def test_read_state_returns_latest_session(repo):
    # The latest session wins even if it is terminal — the canvas needs review
    # to apply/discard.
    s1 = studio.create_session(repo, "auth", "a")
    s2 = studio.create_session(repo, "auth", "b")
    studio.write_session(repo, s1, state=studio.STATE_REVIEW)
    studio.write_session(repo, s2, state=studio.STATE_PLANNING)
    state = studio.read_state(repo)
    assert state["session_id"] == s2
    assert state["state"] == studio.STATE_PLANNING


# --- worktree / apply / discard ---------------------------------------------

def test_make_worktree_apply_discard(repo, tmp_path):
    # The studio copies the repo into a session worktree, applies edits back.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    repo_root = str(tmp_path)

    sid = studio.create_session(repo_root, "auth", "edit")
    wt = studio.make_worktree(repo_root, sid)
    assert os.path.isdir(os.path.join(wt, "app"))

    with open(os.path.join(wt, "app", "x.py"), "w", encoding="utf-8") as fh:
        fh.write("def x():\n    return 2\n")
    changed = studio.apply_session(repo_root, sid)
    assert changed == ["app/x.py"]
    assert "return 2" in (tmp_path / "app" / "x.py").read_text()

    studio.discard_session(repo_root, sid)
    assert not os.path.isdir(studio._worktree_dir(repo_root, sid))


# --- plan endpoint with a fake agent (no LLM) --------------------------------

def _post(port, path, body):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_plan_endpoint_creates_session(server, repo, monkeypatch):
    def fake_run_agent(*, repo_root, session_id, plan, scope_node_id, prompt):
        studio.write_session(repo_root, session_id, state=studio.STATE_PLAN_READY,
                             plan={"nodes": ["auth"], "files": ["auth/login.py"],
                                   "contracts": [], "tests": []})
    monkeypatch.setattr(studio, "_run_agent", fake_run_agent)

    status, body = _post(server, "/api/plan",
                         {"scope_node_id": "auth", "prompt": "add docstring"})
    assert status == 200
    sid = body["session_id"]
    assert sid

    import time
    for _ in range(50):
        state = studio.read_state(repo)
        if state["session_id"] == sid and state["state"] == studio.STATE_PLAN_READY:
            break
        time.sleep(0.02)
    assert state["plan"]["nodes"] == ["auth"]


# --- layout persistence ------------------------------------------------------

def test_layout_defaults(repo):
    layout = studio.read_layout(repo)
    assert layout["view"]["rail_width"] == 420
    assert layout["positions"] == {}


def test_layout_view_merge_keeps_positions(repo):
    studio.write_layout(repo, {"positions": {"n_auth": {"x": 1, "y": 2}}})
    studio.write_layout(repo, {"view": {"rail_width": 640}})
    layout = studio.read_layout(repo)
    assert layout["view"]["rail_width"] == 640
    assert layout["positions"]["n_auth"] == {"x": 1, "y": 2}


def test_layout_positions_merge_keeps_view(repo):
    studio.write_layout(repo, {"view": {"rail_collapsed": True}})
    studio.write_layout(repo, {"positions": {"n_auth": {"x": 1, "y": 2}}})
    layout = studio.read_layout(repo)
    assert layout["view"]["rail_collapsed"] is True
    assert layout["positions"]["n_auth"] == {"x": 1, "y": 2}


def test_layout_positions_null_clears(repo):
    studio.write_layout(repo, {"positions": {"n_auth": {"x": 1, "y": 2}}})
    studio.write_layout(repo, {"positions": None})
    assert studio.read_layout(repo)["positions"] == {}


def test_layout_drops_stale_node_ids(repo):
    studio.write_layout(repo, {"positions": {"n_auth": {"x": 1, "y": 2},
                                            "n_gone": {"x": 9, "y": 9}}})
    layout = studio.read_layout(repo)
    assert "n_auth" in layout["positions"]
    assert "n_gone" not in layout["positions"]


def test_layout_endpoint(server, repo):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{server}/api/layout",
        data=json.dumps({"view": {"rail_width": 520}}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    assert data["layout"]["view"]["rail_width"] == 520
    assert studio.read_layout(repo)["view"]["rail_width"] == 520


# --- review diff (STUDIO.md §9: unified diff grouped by node) ---------------

def _session_with_worktree(root):
    sid = studio.create_session(root, "auth", "edit")
    wt = studio.make_worktree(root, sid)
    return sid, wt


def test_session_diff_groups_modified_file(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    root = str(tmp_path)
    sid, wt = _session_with_worktree(root)

    with open(os.path.join(wt, "app", "x.py"), "w", encoding="utf-8") as fh:
        fh.write("def x():\n    return 2\n")

    data = studio.session_diff(root, sid)
    assert data["changed"] == ["app/x.py"]
    assert data["groups"] and data["groups"][0]["node"] == "Other"
    patch = data["groups"][0]["files"][0]
    assert patch["status"] == "modified"
    assert "return 2" in patch["diff"]


def test_session_diff_shows_untracked_new_file(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    root = str(tmp_path)
    sid, wt = _session_with_worktree(root)

    with open(os.path.join(wt, "app", "new.py"), "w", encoding="utf-8") as fh:
        fh.write("def helper():\n    return 1\n")

    data = studio.session_diff(root, sid)
    files = [f for g in data["groups"] for f in g["files"]]
    new = next(f for f in files if f["path"] == "app/new.py")
    assert new["status"] == "new"
    assert "+def helper()" in new["diff"]
    assert "new file mode" in new["diff"]


def test_api_diff_records_diff_seen(server, repo):
    sid = studio.create_session(repo, None, "task")
    status, body = _get(server, f"/api/diff?session_id={sid}")
    assert status == 200
    data = json.loads(body)
    assert data["changed"] == []
    assert data["groups"] == []
    meta = studio.read_session(repo, sid)
    assert meta["diff_seen"] is True


def test_api_rebuild_recomputes_and_returns_new_version(server, repo, monkeypatch):
    def fake_recompute(root):
        # Simulate a full scan: rewrite graph.json so graph_version changes.
        studio._write_json(studio.graph_path(root),
                           {"version": 1, "repo": {"name": "test"},
                            "nodes": [{"id": "n_auth", "name": "auth", "files": []}],
                            "domains": [], "symbols": [], "node_edges": [],
                            "stats": {"external": []},
                            "extra": "rebuilt"})
        return "ok"

    monkeypatch.setattr(studio, "run_recompute", fake_recompute)
    before = studio.graph_version(repo)
    status, body = _post(server, "/api/rebuild", {})
    assert status == 200
    assert body["ok"] is True
    assert body["graph_version"].startswith("sha256:")
    assert body["graph_version"] != before


def test_api_rebuild_reports_failure(server, repo, monkeypatch):
    def fail_recompute(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(studio, "run_recompute", fail_recompute)
    import urllib.error
    try:
        _post(server, "/api/rebuild", {})
        raise AssertionError("expected HTTP 500")
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode("utf-8"))
    assert body["ok"] is False
    assert "boom" in body["error"]


# --- naming an Unassigned area (STUDIO.md §10, /api/nodes) -------------------

def test_name_area_writes_manifest_with_unique_slug(tmp_path):
    root = str(tmp_path)
    nid = studio.name_area(root, "Comms Layer", ["app/x.py"])
    assert nid == "comms_layer"

    from mindgraph import manifest
    nodes = manifest.load(root)
    assert nodes == [{"id": "comms_layer", "title": "Comms Layer",
                      "own_paths": ["app/x.py"]}]

    # A clashing title gets a unique id instead of overriding the node.
    nid2 = studio.name_area(root, "Comms Layer", ["app/y.py"])
    assert nid2.startswith("comms_layer_")
    assert [n["id"] for n in manifest.load(root)] == ["comms_layer", nid2]


def test_api_nodes_names_area_and_recomputes(server, repo, monkeypatch):
    # A graph with one unassigned symbol; /api/nodes pins its file and triggers
    # a recompute (faked here — the real recompute is emit-level, covered above).
    graph = {"version": 1, "repo": {"name": "test"},
             "nodes": [{"id": "n_unassigned", "name": "Unassigned",
                        "files": ["app/x.py"]}],
             "domains": [], "node_edges": [],
             "symbols": [{"id": "app.x.helper", "file": "app/x.py", "node": "Unassigned"}],
             "stats": {"external": []}}
    studio._write_json(studio.graph_path(repo), graph)

    calls = []
    monkeypatch.setattr(studio, "run_recompute",
                        lambda root: calls.append(root) or "ok")

    status, body = _post(server, "/api/nodes",
                         {"title": "Helpers", "symbol_ids": ["app.x.helper"]})
    assert status == 200
    assert body["ok"] is True
    assert body["node_id"] == "helpers"
    assert body["files"] == ["app/x.py"]
    assert calls == [repo]

    from mindgraph import manifest
    nodes = manifest.load(repo)
    assert nodes == [{"id": "helpers", "title": "Helpers",
                      "own_paths": ["app/x.py"]}]


def test_api_nodes_requires_title_and_known_symbols(server, repo):
    import urllib.error

    def post_err(body):
        try:
            _post(server, "/api/nodes", body)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            return json.loads(e.read().decode("utf-8"))

    assert "title" in post_err({"symbol_ids": ["x"]})["error"]
    assert "symbol_ids" in post_err({"title": "T", "symbol_ids": []})["error"]
    assert "no known symbols" in post_err({"title": "T", "symbol_ids": ["ghost"]})["error"]


# --- file watcher (§14 step 5) -----------------------------------------------

def test_py_snapshot_tracks_py_files_only(tmp_path):
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hi", encoding="utf-8")
    (tmp_path / ".mindvibe").mkdir()
    (tmp_path / ".mindvibe" / "hidden.py").write_text("y = 1", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("z = 1", encoding="utf-8")
    snap = studio._py_snapshot(str(tmp_path))
    assert set(snap) == {"a.py"}


def test_watch_active_pauses_during_agent_session(repo):
    assert studio._watch_active(repo) is False
    sid = studio.create_session(repo, None, "task")
    studio.write_session(repo, sid, state=studio.STATE_PLANNING)
    assert studio._watch_active(repo) is True
    studio.write_session(repo, sid, state=studio.STATE_IDLE)
    assert studio._watch_active(repo) is False


def test_watcher_recomputes_graph_on_file_change(tmp_path):
    import time as _time
    from mindgraph import emit as _emit
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / "main.py").write_text("def main():\n    return 1\n",
                                              encoding="utf-8")
    root = str(tmp_path)
    _emit.load_or_build(root, no_llm=True, force=True)  # seed graph.json
    v0 = studio.graph_version(root)

    studio.start_watcher(root, interval=0.1, debounce=0.15)
    _time.sleep(0.4)  # the watcher settles on the initial snapshot
    (tmp_path / "app" / "extra.py").write_text("def extra():\n    return 2\n",
                                               encoding="utf-8")

    deadline = _time.time() + 20
    while _time.time() < deadline and studio.graph_version(root) == v0:
        _time.sleep(0.25)
    assert studio.graph_version(root) != v0  # the watcher rebuilt graph.json

    with open(studio.graph_path(root), encoding="utf-8") as fh:
        g = json.load(fh)
    assert g["repo"]["files"] >= 2



# /api/node (the runtime per-node description/context endpoint) and
# node_context() were removed in item 7c: descriptions are generated once at
# build time and stored on the graph itself (see test_emit.py's coverage of
# describe.build_descriptions); the viewer never calls a model, so this
# endpoint had no remaining caller. Its tests went with it.
