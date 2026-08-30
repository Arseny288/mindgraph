"""Тесты манифеста стабильных узлов (фаза 1.5, п.5)."""

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
    title: Аутентификация
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
        return {"text": json.dumps({"nodes": [{"name": "Остальное",
                                               "symbols": ids}]},
                                   ensure_ascii=False),
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_tokens": 0, "cache_write_tokens": 0,
                          "raw": {}}}


def test_manifest_load(tmp_path):
    root = _repo_with_manifest(tmp_path)
    nodes = manifest.load(root)
    assert nodes == [{"id": "myauth", "title": "Аутентификация",
                      "own_paths": ["auth/**"]}]


def test_manifest_preassign(tmp_path):
    root = _repo_with_manifest(tmp_path)
    from mindgraph.extract import extract_repo
    g = extract_repo(root)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    pinned = manifest.preassign(real, manifest.load(root))
    # auth/** покрывает auth/login.py и auth/tokens.py
    assert "auth.login.verify_token" in pinned
    assert pinned["auth.login.verify_token"] == "Аутентификация"
    assert "billing.invoice.Invoice" not in pinned


def test_group_repo_pinned_keeps_manifest_nodes(tmp_path):
    from mindgraph.extract import extract_repo
    root = _repo_with_manifest(tmp_path)
    g = extract_repo(root)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    pinned = manifest.preassign(real, manifest.load(root))

    result = group.group_repo(g["symbols"], g["symbol_edges"], _ManifestProvider(),
                              pinned=pinned, existing_nodes=["Аутентификация"])
    names = {n["name"]: set(n["symbols"]) for n in result["nodes"]}
    # пиннутые символы остались в узле «Аутентификация», свободные — в «Остальное»
    assert "auth.login.verify_token" in names["Аутентификация"]
    assert "billing.invoice.Invoice" in names["Остальное"]


def test_build_graph_manifest_gives_stable_ids(tmp_path, monkeypatch):
    root = _repo_with_manifest(tmp_path)
    monkeypatch.setattr(emit, "make_provider", _ManifestProvider)
    monkeypatch.setattr(emit, "assign_domains",
                        lambda *a, **k: {"domains": [], "warnings": [],
                                        "usage": {}, "raw_text": ""})

    graph = emit.build_graph(root, no_llm=False)
    ids = {n["name"]: n["id"] for n in graph["nodes"]}
    assert ids["Аутентификация"] == "myauth"  # id из манифеста, не n_<slug>
    assert "Остальное" in ids
