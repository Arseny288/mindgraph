"""Тесты фазы 1.5, доборы A (unattributed_change) и B (кумулятивный дифф)."""

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


# --- A1: вставка новой функции между символами -> unattributed --------------

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
    assert r["ok"] is True  # не нарушение
    assert r["unattributed_change"]  # но и не тихое ok: новая функция не приписана


# --- A2: правка шапки импортов -> unattributed ------------------------------

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
    assert r["unattributed_change"]  # импорт — не символ


# --- A3: полное удаление чужого символа -> нарушение ------------------------

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


# --- B: кумулятивный дифф, вторая правка трогает чужой символ ---------------

def test_two_sequential_edits_second_touches_foreign_symbol(tmp_path):
    # Базовое состояние; символы bill_util в графе — строки [4,5] (базовые).
    g = _mixed_graph([1, 2], [4, 5])
    node = model.node_by_ref(g, "auth")

    # Правка 1 сдвигает bill_util вниз; правка 2 меняет его сигнатуру.
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
    # bill_util теперь на строках 5-6, но атрибуция по переизвлечённым символам
    # ловит чужой символ, несмотря на устаревшие диапазоны в graph.json.
    assert any(v["reason"] == "foreign_symbol_in_shared_file" and
               v["symbol"] == "m.shared.bill_util" for v in r["violations"])
