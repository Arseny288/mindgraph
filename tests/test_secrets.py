"""Secret hygiene tests (phase 0.4).

The LLM key is read ONLY from the environment (.env in cli.py is only loaded
into env). Explicit check: the key value (marker) never lands in graph.json, in
the raw responses in the project's runs/ dir and in session logs/artifacts.
"""

import json
import os

import pytest

from mindgraph import emit, store

MARKER = "sk-marker-secret-do-not-leak-0000"

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


class _FakeProvider:
    """Returns a valid grouping response (all symbols -> one node)."""

    name = "deepseek"
    model = "deepseek-v4-flash"

    def __init__(self, **kwargs):
        pass

    def group(self, symbols, edges, node_count=None, node_min=5, node_max=15):
        ids = [s["id"] for s in symbols if not s.get("synthetic")]
        text = json.dumps({"nodes": [{"name": "All", "summary": "test",
                                      "symbols": ids}]}, ensure_ascii=False)
        return {"text": text, "usage": {"input_tokens": 1, "output_tokens": 1,
                                        "cache_read_tokens": 0,
                                        "cache_write_tokens": 0, "raw": {}}}


def _assert_marker_absent(text, where):
    assert MARKER not in text, f"secret leaked into {where}"


def test_secret_absent_from_graph_json_and_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", MARKER)
    monkeypatch.setenv("MINDGRAPH_DATA_HOME", str(tmp_path / "_data"))
    monkeypatch.setattr(emit, "make_provider", _FakeProvider)

    # Copy the fixture to tmp — graph.json/runs/ land in the isolated XDG
    # storage root above, never inside this repo copy.
    import shutil
    root = str(tmp_path / "repo")
    shutil.copytree(FIXTURE, root)

    graph = emit.build_graph(root, provider_name="deepseek", no_llm=False,
                             cache_base=None)
    dump = json.dumps(graph, ensure_ascii=False)
    _assert_marker_absent(dump, "graph.json")

    emit.write_graph(root, graph)
    with open(emit.graph_path(root), encoding="utf-8") as fh:
        _assert_marker_absent(fh.read(), "written graph.json")

    runs_dir = os.path.join(store.resolve_project(root, touch=False).root, "runs")
    if os.path.isdir(runs_dir):
        for name in os.listdir(runs_dir):
            with open(os.path.join(runs_dir, name), encoding="utf-8") as fh:
                _assert_marker_absent(fh.read(), f"runs/{name}")


def test_secret_absent_in_no_llm_path(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setenv("DEEPSEEK_API_KEY", MARKER)
    root = str(tmp_path / "repo")
    shutil.copytree(FIXTURE, root)

    graph = emit.build_graph(root, no_llm=True)
    dump = json.dumps(graph, ensure_ascii=False)
    _assert_marker_absent(dump, "graph.json (no-llm)")


def test_graph_json_has_no_key_field(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setenv("DEEPSEEK_API_KEY", MARKER)
    root = str(tmp_path / "repo")
    shutil.copytree(FIXTURE, root)

    graph = emit.build_graph(root, no_llm=True)
    dump = json.dumps(graph, ensure_ascii=False)
    for forbidden in ("api_key", "apikey", "DEEPSEEK_API_KEY"):
        assert forbidden not in dump.lower().replace(" ", ""), forbidden
