"""Phase 1.5 tests, additions A (unattributed_change) and B (cumulative diff)."""

import json
import os

import pytest

from mindgraph import model, verify


def _mixed_graph(auth_lines, bill_lines):
    def sym(i, n, lines):
        return {"id": i, "kind": "function", "file": "m/shared.py",
                "lines": lines, "sig": i.split(".")[-1] + "()", "doc": None,
                "loc": lines[1] - lines[0] + 1, "node": n, "shared": False,
                "owners": [n], "synthetic": False}
    return {
        "version": 2, "grouping": "llm",
        "repo": {"name": "x", "root": "/", "files": 1, "loc": 5,
                 "repo_hash": "h", "generated_at": "t", "config_fingerprint": "c"},
        "symbols": [sym("m.shared.auth_util", "auth", auth_lines),
                    sym("m.shared.bill_util", "billing", bill_lines)],
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


def _write(tmp_path, content):
    p = tmp_path / "m" / "shared.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(tmp_path)


BASE = "def auth_util():\n    pass\n\ndef bill_util():\n    pass\n"


# --- A1: inserting a new function between symbols -> unattributed --------------

def test_insert_new_function_between_symbols_is_unattributed(tmp_path):
    g = _mixed_graph([1, 2], [4, 5])
    node = model.node_by_ref(g, "auth")
    _write(tmp_path,
           "def auth_util():\n    pass\n\n"
           "def new_fn():\n    pass\n\n"
           "def bill_util():\n    pass\n")
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -1,5 +1,8 @@\n"
            " def auth_util():\n     pass\n \n"
            "+def new_fn():\n+    pass\n+\n"
            " def bill_util():\n     pass\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    assert r["ok"] is True  # not a violation
    assert r["unattributed_change"]  # but not a silent ok: the new function is unattributed


# --- A2: editing the import header -> unattributed ------------------------------

def test_import_header_edit_is_unattributed(tmp_path):
    g = _mixed_graph([3, 4], [6, 7])
    node = model.node_by_ref(g, "auth")
    _write(tmp_path,
           "import sys\n\n"
           "def auth_util():\n    pass\n\n"
           "def bill_util():\n    pass\n")
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-import os\n"
            "+import sys\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["unattributed_change"]  # an import — not a symbol


# --- A3: full deletion of a foreign symbol -> violation ------------------------

def test_delete_foreign_symbol_is_violation(tmp_path):
    g = _mixed_graph([1, 2], [4, 5])
    node = model.node_by_ref(g, "auth")
    _write(tmp_path, "def auth_util():\n    pass\n")
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -1,5 +1,2 @@\n"
            " def auth_util():\n     pass\n-\n"
            "-def bill_util():\n-    pass\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    assert r["ok"] is False
    assert any(v["reason"] == "foreign_symbol_in_shared_file" and
               v["symbol"] == "m.shared.bill_util" for v in r["violations"])


# --- B: cumulative diff, a second edit touches a foreign symbol ---------------

def test_two_sequential_edits_second_touches_foreign_symbol(tmp_path):
    # Base state; bill_util's symbols in the graph are lines [4,5] (base).
    g = _mixed_graph([1, 2], [4, 5])
    node = model.node_by_ref(g, "auth")

    # Edit 1 shifts bill_util down; edit 2 changes its signature.
    _write(tmp_path,
           "def auth_util():\n    \"\"\"doc\"\"\"\n    pass\n\n"
           "def bill_util(x):\n    return x\n")
    diff = ("diff --git a/m/shared.py b/m/shared.py\n"
            "--- a/m/shared.py\n"
            "+++ b/m/shared.py\n"
            "@@ -1,5 +1,6 @@\n"
            " def auth_util():\n+    \"\"\"doc\"\"\"\n     pass\n \n"
            "-def bill_util():\n-    pass\n"
            "+def bill_util(x):\n+    return x\n")
    r = verify.verify(g, node, diff, original_dir=str(tmp_path))
    # bill_util is now on lines 5-6, but attribution over re-extracted symbols
    # catches the foreign symbol despite the stale ranges in graph.json.
    assert any(v["reason"] == "foreign_symbol_in_shared_file" and
               v["symbol"] == "m.shared.bill_util" for v in r["violations"])
