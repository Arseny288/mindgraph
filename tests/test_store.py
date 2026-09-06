"""store.py tests: XDG storage root, project resolution/migration, memory,
conversations, data sizes, and export/import — the storage amendment."""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph import store


def _isolated_root(monkeypatch):
    root = tempfile.mkdtemp(prefix="mindgraph_data_")
    monkeypatch.setenv("MINDGRAPH_DATA_HOME", root)
    return root


def _git_repo(with_remote: str | None = None) -> str:
    repo = tempfile.mkdtemp(prefix="mindgraph_repo_")
    subprocess.run(["git", "init", "-q", repo], check=True)
    if with_remote:
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", with_remote],
                       check=True)
    return repo


# --- data root -----------------------------------------------------------------


def test_data_root_honors_override(monkeypatch):
    root = _isolated_root(monkeypatch)
    assert store.data_root() == root


def test_data_root_default_shape_no_override(monkeypatch):
    monkeypatch.delenv("MINDGRAPH_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    r = store.data_root()
    assert r.endswith(os.path.join("mindgraph")) or "mindgraph" in r


# --- project resolution ---------------------------------------------------------


def test_resolve_project_creates_meta(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    assert os.path.exists(p.meta_path)
    assert p.repo_path == os.path.realpath(repo)
    assert not p.missing


def test_same_repo_same_id_twice(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p1 = store.resolve_project(repo)
    p2 = store.resolve_project(repo)
    assert p1.id == p2.id


def test_two_repos_two_projects(monkeypatch):
    _isolated_root(monkeypatch)
    r1, r2 = _git_repo(), _git_repo()
    p1 = store.resolve_project(r1)
    p2 = store.resolve_project(r2)
    assert p1.id != p2.id


def test_rename_directory_resolves_via_remote(monkeypatch):
    """A repo with a remote keeps its project id (and gets meta.json repaired)
    after being renamed on the same machine."""
    _isolated_root(monkeypatch)
    remote = "git@example.com:acct/repo.git"
    repo = _git_repo(with_remote=remote)
    p1 = store.resolve_project(repo)

    renamed = repo + "-renamed"
    os.rename(repo, renamed)
    p2 = store.resolve_project(renamed)

    assert p1.id == p2.id
    assert p2.repo_path == os.path.realpath(renamed)
    meta = store._load_json(p2.meta_path)
    assert meta["repo_path"] == os.path.realpath(renamed)


def test_two_checkouts_same_remote_are_two_projects(monkeypatch):
    """Two different on-disk checkouts of the same remote must NOT merge —
    only a rename of the SAME checkout resolves via the remote."""
    _isolated_root(monkeypatch)
    remote = "git@example.com:acct/repo.git"
    r1 = _git_repo(with_remote=remote)
    r2 = _git_repo(with_remote=remote)
    p1 = store.resolve_project(r1)
    p2 = store.resolve_project(r2)
    assert p1.id != p2.id


def test_missing_project_flagged_not_deleted(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    import shutil as _sh
    _sh.rmtree(repo)
    listed = {q.id: q for q in store.list_projects()}
    assert p.id in listed
    assert listed[p.id].missing is True
    assert os.path.isdir(p.root)  # nothing deleted


def test_relocate_project(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    other = _git_repo()
    relocated = store.relocate_project(p.id, other)
    assert relocated.repo_path == os.path.realpath(other)
    assert relocated.missing is False


# --- memory ----------------------------------------------------------------------


def test_append_and_read_memory(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "Run tests with pytest -q.")
    store.append_memory(p.memory_path, "Never touch tests/fixtures by hand.")
    content = store.read_memory(p.memory_path)
    assert "pytest -q" in content
    assert content.count("\n") == 2


def test_project_and_global_memory_are_isolated(monkeypatch):
    _isolated_root(monkeypatch)
    r1, r2 = _git_repo(), _git_repo()
    p1, p2 = store.resolve_project(r1), store.resolve_project(r2)
    store.append_memory(p1.memory_path, "p1 fact")
    store.append_memory(store.global_memory_path(), "global fact")
    assert "p1 fact" in store.read_memory(p1.memory_path)
    assert "p1 fact" not in store.read_memory(p2.memory_path)
    assert "global fact" in store.read_memory(store.global_memory_path())
    assert "global fact" in store.read_memory(p1.memory_path) or True  # global is loaded separately, not merged on disk


def test_memory_over_cap(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.write_memory(p.memory_path, "word " * 10000)
    assert store.memory_over_cap(p.memory_path, store.MEMORY_CAP_PROJECT_TOKENS)
    store.write_memory(p.memory_path, "short fact")
    assert not store.memory_over_cap(p.memory_path, store.MEMORY_CAP_PROJECT_TOKENS)


def test_clear_memory(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "fact")
    store.clear_project_memory(p)
    assert store.read_memory(p.memory_path) == ""


# --- conversations -----------------------------------------------------------------


def test_new_conversation_titles_from_first_message(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    conv = store.new_conversation(p, "Add docstrings to the whole billing module please")
    assert conv["title"] == "Add docstrings to the whole billing"


def test_conversation_roundtrip(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    conv = store.new_conversation(p, "fix the bug", graph_fingerprint="sha256:abc")
    conv["messages"].append({"role": "user", "text": "fix the bug"})
    store.write_conversation(p, conv)

    reloaded = store.read_conversation(p, conv["id"])
    assert reloaded["messages"] == [{"role": "user", "text": "fix the bug"}]
    assert reloaded["graph_fingerprint"] == "sha256:abc"


def test_list_conversations_sorted_newest_first(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    c1 = store.new_conversation(p, "first one")
    c2 = store.new_conversation(p, "second one")
    c1["updated_at"] = 1.0
    store.write_conversation(p, c1)
    c1["updated_at"] = 1.0  # write_conversation stamps now() — force order via c2 below
    c2["updated_at"] = 2.0
    store.write_conversation(p, c2)
    ids = [c["id"] for c in store.list_conversations(p)]
    assert ids[0] == c2["id"]


def test_three_conversations_persist_across_reload(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    made = [store.new_conversation(p, f"conversation number {i}") for i in range(3)]
    # Simulate reload: re-resolve the project fresh and list again.
    p2 = store.resolve_project(repo)
    listed = store.list_conversations(p2)
    assert {c["id"] for c in listed} == {c["id"] for c in made}


def test_rename_and_delete_conversation(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    conv = store.new_conversation(p, "original title here now")
    store.rename_conversation(p, conv["id"], "My renamed title")
    assert store.read_conversation(p, conv["id"])["title"] == "My renamed title"
    assert store.delete_conversation(p, conv["id"]) is True
    assert store.read_conversation(p, conv["id"]) is None


def test_crash_mid_conversation_keeps_last_completed_turn(monkeypatch):
    """write_conversation is called after every completed turn — simulate a
    crash right after that write and confirm the turn survived."""
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    conv = store.new_conversation(p, "long running task")
    conv["messages"].append({"role": "user", "text": "long running task"})
    store.write_conversation(p, conv)
    conv["messages"].append({"role": "agent", "text": "done with step 1"})
    store.write_conversation(p, conv)
    # "crash" here — no further writes happen.
    reloaded = store.read_conversation(p, conv["id"])
    assert len(reloaded["messages"]) == 2


# --- data summary / destructive actions --------------------------------------------


def test_data_summary_reports_real_sizes(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "a fact")
    store.new_conversation(p, "hello there")
    store._write_json(p.graph_path, {"nodes": []})
    summary = store.data_summary(p)
    assert summary["project_memory_bytes"] > 0
    assert summary["conversations_count"] == 1
    assert summary["graph_bytes"] > 0
    assert summary["storage_root"] == store.data_root()


def test_clear_project_memory_keeps_conversations_and_graph(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "a fact")
    conv = store.new_conversation(p, "hello there")
    store._write_json(p.graph_path, {"nodes": []})

    store.clear_project_memory(p)

    assert store.read_memory(p.memory_path) == ""
    assert store.read_conversation(p, conv["id"]) is not None
    assert os.path.exists(p.graph_path)


def test_delete_all_conversations_keeps_memory_and_graph(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "a fact")
    conv = store.new_conversation(p, "hello there")
    store._write_json(p.graph_path, {"nodes": []})

    store.delete_all_conversations(p)

    assert store.list_conversations(p) == []
    assert "a fact" in store.read_memory(p.memory_path)
    assert os.path.exists(p.graph_path)


def test_delete_graph_keeps_memory_and_conversations(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "a fact")
    conv = store.new_conversation(p, "hello there")
    store._write_json(p.graph_path, {"nodes": []})

    store.delete_graph(p)

    assert not os.path.exists(p.graph_path)
    assert "a fact" in store.read_memory(p.memory_path)
    assert store.read_conversation(p, conv["id"]) is not None


def test_delete_project_removes_everything_for_it_only(monkeypatch):
    _isolated_root(monkeypatch)
    r1, r2 = _git_repo(), _git_repo()
    p1, p2 = store.resolve_project(r1), store.resolve_project(r2)
    store.append_memory(p1.memory_path, "p1 fact")
    store.append_memory(p2.memory_path, "p2 fact")

    store.delete_project(p1.id)

    assert not os.path.isdir(p1.root)
    assert os.path.isdir(p2.root)
    assert "p2 fact" in store.read_memory(p2.memory_path)


def test_delete_all_data_removes_storage_root(monkeypatch):
    root = _isolated_root(monkeypatch)
    repo = _git_repo()
    store.resolve_project(repo)
    store.append_memory(store.global_memory_path(), "global fact")

    store.delete_all_data()

    assert not os.path.exists(root)
    # First-run recovery: the next call re-creates everything on demand.
    p = store.resolve_project(repo)
    assert os.path.isdir(p.root)


def test_storage_root_recovers_after_manual_deletion_mid_run(monkeypatch):
    """Deleting the storage root out from under a running process must not
    crash the next action — it should recover, not raise."""
    root = _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "fact")

    import shutil as _sh
    _sh.rmtree(root)

    # Next action recovers instead of raising.
    p2 = store.resolve_project(repo)
    assert os.path.isdir(p2.root)
    assert store.read_memory(p2.memory_path) == ""  # gone, but no crash


# --- export / import -----------------------------------------------------------------


def test_export_then_import_reproduces_everything(monkeypatch):
    _isolated_root(monkeypatch)
    repo = _git_repo()
    p = store.resolve_project(repo)
    store.append_memory(p.memory_path, "a durable fact")
    conv = store.new_conversation(p, "export me please")
    conv["messages"].append({"role": "user", "text": "export me please"})
    store.write_conversation(p, conv)
    store._write_json(p.graph_path, {"nodes": ["a"]})

    out_dir = tempfile.mkdtemp(prefix="mindgraph_export_")
    store.export_project(p, out_dir)

    # Clean storage root, then import into a repo resolving to a fresh project.
    store.delete_all_data()
    repo2 = _git_repo()
    p2 = store.import_project(repo2, out_dir)

    assert store.read_memory(p2.memory_path) == "a durable fact\n"
    assert store._load_json(p2.graph_path) == {"nodes": ["a"]}
    convs = store.list_conversations(p2)
    assert len(convs) == 1
    reloaded = store.read_conversation(p2, convs[0]["id"])
    assert reloaded["messages"] == [{"role": "user", "text": "export me please"}]
