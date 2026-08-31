"""Тесты фазы 1.5: смешанные файлы, символьный verify, allow_create,
вердикты тестов и неполнота графа.
"""

import json
import os

import pytest

from mindgraph import emit, model, verify, impact, testselect

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


@pytest.fixture()
def graph():
    return emit.build_graph(FIXTURE, no_llm=True)


def _mixed_graph():
    """Синтетический граф со смешанным файлом m/shared.py (auth + billing)."""
    sym = lambda i, n, lo, hi: {
        "id": i, "kind": "function", "file": "m/shared.py",
        "lines": [lo, hi], "sig": f"{i.split('.')[-1]}()", "doc": None,
        "loc": hi - lo + 1, "node": n, "shared": False, "owners": [n],
        "synthetic": False,
    }
    return {
        "version": 2, "grouping": "llm",
        "repo": {"name": "x", "root": "/", "files": 1, "loc": 5,
                 "repo_hash": "h", "generated_at": "t",
                 "config_fingerprint": "c"},
        "symbols": [
            sym("m.shared.auth_util", "auth", 1, 2),
            sym("m.shared.bill_util", "billing", 4, 5),
        ],
        "symbol_edges": [],
        "nodes": [
            {"id": "n_auth", "name": "auth", "summary": "",
             "symbols": ["m.shared.auth_util"], "files": ["m/shared.py"],
             "loc": 2, "shared_count": 0},
            {"id": "n_billing", "name": "billing", "summary": "",
             "symbols": ["m.shared.bill_util"], "files": ["m/shared.py"],
             "loc": 2, "shared_count": 0},
        ],
        "node_edges": [], "domains": [], "domain_edges": [],
        "stats": {"symbols": 2, "unassigned": 0, "shared_symbols": 0,
                  "shared_ratio": 0.0, "external": []},
        "warnings": [],
    }


# --- 1. смешанные файлы -----------------------------------------------------

def test_mixed_file_stats_metric(graph):
    # фикстура не содержит смешанных файлов (каждый файл — один пакет)
    s = model.mixed_file_stats(graph)
    assert s["mixed_files"] == 0


def test_mixed_file_detection():
    g = _mixed_graph()
    mixed = model.mixed_files(g)
    assert "m/shared.py" in mixed
    # tie-break: при 1:1 хозяин — лексикографически первый узел "auth"
    assert mixed["m/shared.py"]["owner"] == "auth"
    assert mixed["m/shared.py"]["foreign"] == {"billing": ["m.shared.bill_util"]}


def test_node_view_lists_mixed_and_foreign_symbols():
    g = _mixed_graph()
    node = model.node_by_ref(g, "auth")
    view = model.node_view(g, node)
    assert view["mixed"] == [{
        "file": "m/shared.py", "owner": "auth",
        "foreign_symbols": ["m.shared.bill_util"],
        "foreign_nodes": ["billing"],
        "foreign_ranges": [{"symbol": "m.shared.bill_util", "node": "billing",
                            "lines": [4, 5]}],
    }]


def test_boundary_lists_mixed_and_allow_create():
    g = _mixed_graph()
    node = model.node_by_ref(g, "auth")
    b = model.boundary(g, node)
    assert b["mixed"]  # смешанный файл перечислен
    assert b["allow_create"] == ["m/**"]  # каталог целиком принадлежит узлу


# --- символьный verify ------------------------------------------------------

def _write_new(tmp_path, content):
    p = tmp_path / "m" / "shared.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(tmp_path)


_BASE_SHARED = "def auth_util():\n    pass\n\ndef bill_util():\n    pass\n"


def test_verify_foreign_symbol_in_mixed_file_is_violation(tmp_path):
    g = _mixed_graph()
    node = model.node_by_ref(g, "auth")
    _write_new(tmp_path,
               "def auth_util():\n    pass\n\ndef bill_util(x):\n    pass\n")
    # дифф трогает строку 4 — символ billing в смешанном файле зоны
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -4,1 +4,1 @@\n"
            "-def bill_util():\n"
            "+def bill_util(x):\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    assert r["ok"] is False
    assert any(v["reason"] == "foreign_symbol_in_shared_file" and
               v["symbol"] == "m.shared.bill_util" for v in r["violations"])


def test_verify_own_symbol_in_mixed_file_is_ok(tmp_path):
    g = _mixed_graph()
    node = model.node_by_ref(g, "auth")
    _write_new(tmp_path,
               "def auth_util(x):\n    pass\n\ndef bill_util():\n    pass\n")
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-def auth_util():\n"
            "+def auth_util(x):\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    assert r["ok"] is True


# --- 4. allow_create --------------------------------------------------------

def test_allow_create_globs_fixture(graph):
    node = model.node_by_ref(graph, "auth")
    assert model.allow_create_globs(graph, node) == ["auth/**"]


# --- 2. вердикты тестов -----------------------------------------------------

def test_tests_empty_is_unverified(graph):
    node = model.node_by_ref(graph, "auth")
    r = testselect.node_tests(graph, node, "for-node auth")
    assert r["status"] == model.STATUS_UNVERIFIED
    assert r["selection_confidence"] == 0.0


# --- 3. неполнота графа -----------------------------------------------------

def test_impact_reports_unresolved_and_incomplete(graph):
    r = impact.impact(graph, "auth.login.verify_token")
    assert r["unresolved"] == ["os"]
    assert r["complete"] is False


def test_verify_reports_unresolved(graph):
    node = model.node_by_ref(graph, "auth")
    diff = ("diff --git a/auth/login.py b/auth/login.py\n"
            "--- a/auth/login.py\n"
            "+++ b/auth/login.py\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n")
    r = verify.verify(graph, node, diff, original_dir=FIXTURE)
    assert r["unresolved"] == ["os"]
    assert r["complete"] is False
