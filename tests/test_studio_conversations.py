"""Studio conversation/memory/data API tests (items 2, 3b, and the settings
"Data" section) — the storage amendment's HTTP surface on top of store.py."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from mindgraph import studio


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path_factory, monkeypatch):
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
        return r.status, json.loads(r.read())


def _post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


# --- conversations: direct function API --------------------------------------


def test_no_active_conversation_initially(repo):
    listing = studio.list_conversations_api(repo)
    assert listing["conversations"] == []
    assert listing["active_id"] is None


def test_ensure_active_conversation_creates_one(repo):
    conv = studio.ensure_active_conversation(repo, first_message="Fix the login bug")
    assert conv["title"] == "Fix the login bug"
    assert studio.get_active_conversation_id(repo) == conv["id"]
    # Calling again returns the SAME conversation (still active).
    conv2 = studio.ensure_active_conversation(repo, first_message="unrelated")
    assert conv2["id"] == conv["id"]


def test_append_turn_persists_and_titles_conversation(repo):
    conv = studio.new_conversation_api(repo)
    sid = studio.create_session(repo, "auth", "Add docstrings everywhere please")
    studio.append_turn_to_conversation(repo, conv["id"], sid,
                                       prompt="Add docstrings everywhere please")
    full = studio.get_conversation_api(repo, conv["id"])
    assert full["title"] == "Add docstrings everywhere please"
    assert len(full["turns"]) == 1
    assert full["turns"][0]["id"] == sid


def test_three_conversations_switch_and_persist(repo):
    c1 = studio.new_conversation_api(repo)
    c2 = studio.new_conversation_api(repo)
    c3 = studio.new_conversation_api(repo)
    assert studio.get_active_conversation_id(repo) == c3["id"]

    switched = studio.switch_conversation_api(repo, c1["id"])
    assert switched["id"] == c1["id"]
    assert studio.get_active_conversation_id(repo) == c1["id"]

    listing = studio.list_conversations_api(repo)["conversations"]
    assert {c["id"] for c in listing} == {c1["id"], c2["id"], c3["id"]}


def test_rename_and_delete_conversation_api(repo):
    conv = studio.new_conversation_api(repo)
    renamed = studio.rename_conversation_api(repo, conv["id"], "My title")
    assert renamed["title"] == "My title"
    assert studio.delete_conversation_api(repo, conv["id"]) is True
    assert studio.get_conversation_api(repo, conv["id"]) is None
    # Deleting the active conversation clears the active pointer.
    assert studio.get_active_conversation_id(repo) is None


def test_conversation_stale_when_graph_rebuilt(repo):
    conv = studio.new_conversation_api(repo)
    v1 = studio.graph_version(repo)
    assert not studio.get_conversation_api(repo, conv["id"])["stale"]

    studio._write_json(studio.graph_path(repo), {"version": 1, "repo": {"name": "x"},
                                                 "nodes": [], "domains": [],
                                                 "symbols": [], "node_edges": [],
                                                 "stats": {"external": []}})
    assert studio.graph_version(repo) != v1
    assert studio.get_conversation_api(repo, conv["id"])["stale"]


# --- conversations: HTTP surface ----------------------------------------------


def test_plan_endpoint_creates_conversation_and_attaches_turn(server, repo, monkeypatch):
    def fake_run_agent(*, repo_root, session_id, plan, scope_node_id, prompt):
        studio.write_session(repo_root, session_id, state=studio.STATE_PLAN_READY,
                             plan={"nodes": [], "files": [], "contracts": [], "tests": []})
    monkeypatch.setattr(studio, "_run_agent", fake_run_agent)

    status, body = _post(server, "/api/plan", {"scope_node_id": None, "prompt": "hello there"})
    assert status == 200
    cid = body["conversation_id"]
    assert cid

    status, listing = _get(server, "/api/conversations")
    assert listing["active_id"] == cid
    assert len(listing["conversations"]) == 1

    status, conv = _get(server, f"/api/conversation?id={cid}")
    assert conv["title"] == "hello there"
    assert len(conv["turns"]) == 1


def test_conversations_http_crud(server, repo):
    status, c1 = _post(server, "/api/conversations", {})
    status, c2 = _post(server, "/api/conversations", {})
    assert status == 200

    status, listing = _get(server, "/api/conversations")
    assert {c["id"] for c in listing["conversations"]} == {c1["id"], c2["id"]}

    status, sw = _post(server, "/api/conversations/switch", {"conversation_id": c1["id"]})
    assert sw["conversation"]["id"] == c1["id"]

    status, ren = _post(server, "/api/conversations/rename",
                        {"conversation_id": c1["id"], "title": "Renamed"})
    assert ren["conversation"]["title"] == "Renamed"

    status, dele = _post(server, "/api/conversations/delete", {"conversation_id": c2["id"]})
    assert dele["ok"] is True

    status, listing = _get(server, "/api/conversations")
    assert [c["id"] for c in listing["conversations"]] == [c1["id"]]


def test_unknown_conversation_404(server, repo):
    try:
        _get(server, "/api/conversation?id=ghost")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


# --- memory --------------------------------------------------------------------


def test_memory_write_read_clear_http(server, repo):
    status, body = _post(server, "/api/memory",
                         {"scope": "project", "content": "Run tests with pytest -q.\n"})
    assert body["ok"] is True

    status, body = _get(server, "/api/memory?scope=project")
    assert body["content"] == "Run tests with pytest -q.\n"

    status, body = _post(server, "/api/memory/clear", {"scope": "project"})
    assert body["ok"] is True
    status, body = _get(server, "/api/memory?scope=project")
    assert body["content"] == ""


def test_memory_scopes_isolated_http(server, repo):
    _post(server, "/api/memory", {"scope": "project", "content": "project fact\n"})
    _post(server, "/api/memory", {"scope": "global", "content": "global fact\n"})
    _, proj = _get(server, "/api/memory?scope=project")
    _, glob = _get(server, "/api/memory?scope=global")
    assert proj["content"] == "project fact\n"
    assert glob["content"] == "global fact\n"


def test_remember_appends_and_undo(server, repo):
    status, body = _post(server, "/api/remember",
                         {"scope": "project", "line": "Never touch fixtures by hand."})
    assert body["ok"] is True
    entry = body["line"]
    assert "Never touch fixtures by hand." in entry

    _, mem = _get(server, "/api/memory?scope=project")
    assert mem["content"] == entry

    status, undo = _post(server, "/api/remember/undo", {"scope": "project", "line": entry})
    assert undo["ok"] is True
    _, mem = _get(server, "/api/memory?scope=project")
    assert mem["content"] == ""


def test_remember_undo_refuses_if_not_last_line(server, repo):
    _post(server, "/api/remember", {"scope": "project", "line": "first fact"})
    _post(server, "/api/remember", {"scope": "project", "line": "second fact"})
    status, undo = _post(server, "/api/remember/undo",
                         {"scope": "project", "line": "first fact\n"})
    assert undo["ok"] is False
    _, mem = _get(server, "/api/memory?scope=project")
    assert "first fact" in mem["content"] and "second fact" in mem["content"]


# --- data summary / destructive actions ---------------------------------------


def test_data_summary_http(server, repo):
    _post(server, "/api/remember", {"scope": "project", "line": "a fact"})
    _post(server, "/api/conversations", {})
    status, body = _get(server, "/api/data")
    assert status == 200
    assert body["conversations_count"] == 1
    assert body["project_memory_bytes"] > 0
    assert body["graph_bytes"] > 0


def test_data_clear_conversations_keeps_memory_and_graph(server, repo):
    _post(server, "/api/remember", {"scope": "project", "line": "a fact"})
    _post(server, "/api/conversations", {})
    status, body = _post(server, "/api/data/clear", {"target": "conversations"})
    assert body["ok"] is True

    _, listing = _get(server, "/api/conversations")
    assert listing["conversations"] == []
    _, mem = _get(server, "/api/memory?scope=project")
    assert "a fact" in mem["content"]
    status, _ = _get(server, "/api/graph")
    assert status == 200


def test_data_clear_project_memory_only(server, repo):
    _post(server, "/api/remember", {"scope": "project", "line": "a fact"})
    _post(server, "/api/conversations", {})
    _post(server, "/api/data/clear", {"target": "project_memory"})

    _, mem = _get(server, "/api/memory?scope=project")
    assert mem["content"] == ""
    _, listing = _get(server, "/api/conversations")
    assert len(listing["conversations"]) == 1


def test_data_clear_unknown_target_400(server, repo):
    try:
        _post(server, "/api/data/clear", {"target": "nonsense"})
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
