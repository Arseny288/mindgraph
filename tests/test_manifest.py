"""Stable-node manifest tests (phase 1.5, item 5)."""

import json
import os
import shutil

import pytest

from mindgraph import emit, manifest, group

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")

_MANIFEST = """\
nodes:
  - id: myauth
    title: Authentication
    own_paths: ["auth/**"]
"""


def _repo_with_manifest(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    (root / ".mindvibe").mkdir()
    (root / ".mindvibe" / "nodes.yaml").write_text(_MANIFEST, encoding="utf-8")
    return str(root)


class _ManifestProvider:
    name = "deepseek"
    model = "test"

    def __init__(self, *a, **k):
        pass

    def group(self, symbols, edges, node_count=None, node_min=5, node_max=15):
        return self.complete("", json.dumps({"symbols": [{"id": s["id"]} for s in symbols]}))

    def complete(self, system, user, max_tokens=None):
        ids = [s["id"] for s in json.loads(user)["symbols"]]
        return {"text": json.dumps({"nodes": [{"id": "", "title": "Remaining",
                                                "symbols": ids}]},
                                   ensure_ascii=False),
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_tokens": 0, "cache_write_tokens": 0,
                          "raw": {}}}


def test_manifest_load(tmp_path):
    root = _repo_with_manifest(tmp_path)
    nodes = manifest.load(root)
    assert nodes == [{"id": "myauth", "title": "Authentication",
                      "own_paths": ["auth/**"]}]


def test_manifest_preassign(tmp_path):
    root = _repo_with_manifest(tmp_path)
    from mindgraph.extract import extract_repo
    g = extract_repo(root)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    pinned = manifest.preassign(real, manifest.load(root))
    # auth/** covers auth/login.py and auth/tokens.py; assignment by STABLE id
    assert "auth.login.verify_token" in pinned
    assert pinned["auth.login.verify_token"] == "myauth"
    assert "billing.invoice.Invoice" not in pinned


def test_group_repo_pinned_keeps_manifest_nodes(tmp_path):
    from mindgraph.extract import extract_repo
    root = _repo_with_manifest(tmp_path)
    g = extract_repo(root)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    pinned = manifest.preassign(real, manifest.load(root))

    result = group.group_repo(g["symbols"], g["symbol_edges"], _ManifestProvider(),
                              pinned=pinned,
                              existing_nodes=[{"id": "myauth",
                                                "title": "Authentication"}])
    names = {n["name"]: set(n["symbols"]) for n in result["nodes"]}
    # pinned symbols stayed in the «Authentication» node, free ones — in «Remaining»
    assert "auth.login.verify_token" in names["Authentication"]
    assert "billing.invoice.Invoice" in names["Remaining"]


def test_build_graph_manifest_gives_stable_ids(tmp_path, monkeypatch):
    root = _repo_with_manifest(tmp_path)
    monkeypatch.setattr(emit, "make_provider", _ManifestProvider)
    monkeypatch.setattr(emit, "assign_domains",
                        lambda *a, **k: {"domains": [], "warnings": [],
                                        "usage": {}, "raw_text": ""})

    graph = emit.build_graph(root, no_llm=False)
    ids = {n["name"]: n["id"] for n in graph["nodes"]}
    assert ids["Authentication"] == "myauth"  # id from the manifest, not n_<slug>
    assert "Remaining" in ids


class _DriftProvider(_ManifestProvider):
    """Returns an existing node with a CHANGED title but the same id."""

    def complete(self, system, user, max_tokens=None):
        ids = [s["id"] for s in json.loads(user)["symbols"]]
        return {"text": json.dumps(
            {"nodes": [{"id": "myauth",
                        "title": "Authentication and tokens",
                        "symbols": ids}]}, ensure_ascii=False),
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "cache_read_tokens": 0, "cache_write_tokens": 0,
                      "raw": {}}}


def test_manifest_identity_is_by_id_not_title(tmp_path):
    # Title drift must NOT create a duplicate: free symbols land in the existing
    # myauth node (by id), the title is refreshed.
    from mindgraph.extract import extract_repo
    root = _repo_with_manifest(tmp_path)
    g = extract_repo(root)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    pinned = manifest.preassign(real, manifest.load(root))

    result = group.group_repo(g["symbols"], g["symbol_edges"], _DriftProvider(),
                              pinned=pinned,
                              existing_nodes=[{"id": "myauth",
                                                "title": "Authentication"}])
    names = {n["name"]: set(n["symbols"]) for n in result["nodes"]}
    # one node, not two: the free symbol was assigned by id, the title was refined
    assert "Authentication and tokens" in names
    assert "billing.invoice.Invoice" in names["Authentication and tokens"]
    assert "Authentication" not in names
