"""Phase 2 tests (the Python part): deterministic serialization, resolving
.mindvibe/ from a worktree, node memory, foreign symbol ranges for edit_file.
"""

import json
import os
import subprocess
import sys

import pytest

from mindgraph import emit, manifest, model

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


@pytest.fixture(scope="module", autouse=True)
def _scanned_fixture():
    """test_cli_node_output_byte_stable reads an already-built graph.json —
    scan once, into an isolated storage root (storage amendment)."""
    import tempfile
    old = os.environ.get("MINDGRAPH_DATA_HOME")
    os.environ["MINDGRAPH_DATA_HOME"] = tempfile.mkdtemp(prefix="mindgraph_test_data_")
    try:
        subprocess.run([sys.executable, "-m", "mindgraph.cli", "scan", FIXTURE, "--no-llm"],
                       capture_output=True, text=True, check=True)
        yield
    finally:
        if old is None:
            os.environ.pop("MINDGRAPH_DATA_HOME", None)
        else:
            os.environ["MINDGRAPH_DATA_HOME"] = old


@pytest.fixture()
def graph():
    return emit.build_graph(FIXTURE, no_llm=True)


# --- 2. deterministic serialization of a node context -----------------------

def test_node_context_serialization_is_deterministic(graph):
    node = model.node_by_ref(graph, "auth")
    v1 = model.node_view(graph, node)
    v2 = model.node_view(graph, node)
    s1 = json.dumps(v1, ensure_ascii=False, sort_keys=True)
    s2 = json.dumps(v2, ensure_ascii=False, sort_keys=True)
    assert s1 == s2


def test_cli_node_output_byte_stable():
    def run():
        return subprocess.run(
            [sys.executable, "-m", "mindgraph.cli", "node", "auth",
             "--json", "--dir", FIXTURE],
            capture_output=True, text=True).stdout

    a, b = run(), run()
    assert a == b
    assert json.loads(a)["schema_version"] == "1"


# --- 3. resolving .mindvibe/ to the main repository ---------------------------

def test_mindvibe_root_resolves_worktree_to_main_repo(tmp_path):
    main = tmp_path / "main"
    (main / ".mindvibe").mkdir(parents=True)
    (main / ".mindvibe" / "nodes.yaml").write_text(
        "nodes:\n  - id: auth\n    title: Authentication\n    own_paths: [\"auth/**\"]\n",
        encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/wt\n", encoding="utf-8")

    assert manifest.mindvibe_root(str(wt)) == str(main)
    # the manifest is found OUTSIDE the worktree
    nodes = manifest.load(str(wt))
    assert nodes == [{"id": "auth", "title": "Authentication",
                      "own_paths": ["auth/**"]}]


def test_mindvibe_root_no_worktree_returns_self(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert manifest.mindvibe_root(str(d)) == str(d)


# --- 5. node memory --------------------------------------------------------

def test_node_notes_read_write(tmp_path):
    root = str(tmp_path)
    assert manifest.read_note(root, "auth") is None
    path = manifest.write_note(root, "auth", "remember: check tokens")
    assert path.endswith(".mindvibe/notes/auth.md")
    assert manifest.read_note(root, "auth") == "remember: check tokens"
    assert manifest.read_note(root, "billing") is None


def test_node_notes_resolve_to_main_repo_from_worktree(tmp_path):
    main = tmp_path / "main"
    (main / ".mindvibe" / "notes").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/wt\n", encoding="utf-8")

    manifest.write_note(str(wt), "auth", "note")
    assert manifest.read_note(str(main), "auth") == "note"


# --- 1. foreign symbol ranges for edit_file ------------------------------

def test_foreign_symbol_ranges(graph):
    # the fixture has no mixed files — the synthetic case below
    from tests.test_phase15 import _mixed_graph
    g = _mixed_graph()
    ranges = model.foreign_symbol_ranges(g, "m/shared.py")
    assert ranges == [{"symbol": "m.shared.bill_util", "node": "billing",
                       "lines": [4, 5]}]
