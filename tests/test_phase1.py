"""Тесты фазы 1: модель владения, impact, verify и JSON-контракт CLI.

Всё на фикстуре sample_repo через --no-llm (package-proxy): узлы auth, billing,
common, cycle. common.utils — общий shared-слой.
"""

import json
import os
import subprocess
import sys

import pytest

from mindgraph import emit, model, verify, impact, testselect

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


@pytest.fixture()
def graph():
    return emit.build_graph(FIXTURE, no_llm=True)


# --- модель владения -------------------------------------------------------

def test_file_owner_map(graph):
    owners = model.file_owner_map(graph)
    assert owners["auth/login.py"] == "auth"
    assert owners["billing/invoice.py"] == "billing"
    assert owners["common/utils.py"] == "common"


def test_contract_symbols(graph):
    contracts = {c["symbol"] for c in model.contract_symbols(graph)}
    assert "common.utils.hash_value" in contracts
    assert "common.utils.CACHE" in contracts
    assert "auth.login.verify_token" not in contracts


def test_node_view_owned_contracts_foreign(graph):
    node = model.node_by_ref(graph, "auth")
    view = model.node_view(graph, node)
    assert set(view["owned_files"]) == {"auth/login.py", "auth/tokens.py"}
    assert "auth.login.login" in view["owned_symbols"]
    # auth зависит от shared common.utils.* — это контракты узла
    contract_ids = {c["symbol"] for c in view["contracts"]}
    assert "common.utils.hash_value" in contract_ids
    # чужое — файл биллинга
    assert "billing/invoice.py" in view["foreign_files"]


def test_boundary_globs(graph):
    node = model.node_by_ref(graph, "auth")
    b = model.boundary(graph, node)
    assert b["schema_version"] == "1"
    assert "auth/**" in b["allow_write"]
    assert b["deny_write"] == ["**"]
    # контрактные символы лежат в common/ — их можно читать
    assert any(g.startswith("common") for g in b["allow_read"])
    assert all("signature_hash" in c for c in b["contracts"])


# --- impact ----------------------------------------------------------------

def test_impact_symbol(graph):
    r = impact.impact(graph, "auth.login.verify_token")
    assert r["query"] == "auth.login.verify_token"
    assert "auth.login.verify_token" in r["symbols"]


def test_impact_file_depth_one(graph):
    r = impact.impact(graph, "common/utils.py", depth=1)
    # прямые потомки hash_value/normalize/CACHE
    assert r["depth"] == 1
    assert all(s in r["files"] for s in [])


def test_impact_unknown_query(graph):
    r = impact.impact(graph, "no.such.symbol")
    assert r["symbols"] == [] and r["files"] == []


# --- verify ----------------------------------------------------------------

def test_verify_foreign_file_is_violation(graph):
    diff = ("diff --git a/billing/invoice.py b/billing/invoice.py\n"
            "--- a/billing/invoice.py\n"
            "+++ b/billing/invoice.py\n"
            "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n")
    node = model.node_by_ref(graph, "auth")
    r = verify.verify(graph, node, diff, original_dir=FIXTURE)
    assert r["ok"] is False
    assert any(v["reason"] == "outside_node_zone" and
               v["path"] == "billing/invoice.py" for v in r["violations"])


def test_verify_in_zone_no_change_is_ok(graph):
    diff = ("diff --git a/auth/login.py b/auth/login.py\n"
            "--- a/auth/login.py\n"
            "+++ b/auth/login.py\n"
            "@@ -10,1 +10,1 @@\n-    return issue(len(name))\n+    return issue(len(name) + 0)\n")
    node = model.node_by_ref(graph, "auth")
    r = verify.verify(graph, node, diff, original_dir=FIXTURE)
    assert r["ok"] is True


def test_verify_contract_signature_change_is_violation(graph):
    diff = ("diff --git a/common/utils.py b/common/utils.py\n"
            "--- a/common/utils.py\n"
            "+++ b/common/utils.py\n"
            "@@ -6,1 +6,1 @@\n"
            "-def hash_value(v: str) -> str:\n"
            "+def hash_value(v: str, salt: str = \"\") -> str:\n")
    node = model.node_by_ref(graph, "common")  # владеет common/utils.py
    r = verify.verify(graph, node, diff, original_dir=FIXTURE)
    assert r["ok"] is False
    assert any(v["reason"] == "contract_signature_changed" and
               v["symbol"] == "common.utils.hash_value" for v in r["violations"])


def test_verify_dotdot_path_is_rejected(graph):
    diff = ("diff --git a/../etc/passwd b/../etc/passwd\n"
            "--- a/../etc/passwd\n"
            "+++ b/../etc/passwd\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n")
    node = model.node_by_ref(graph, "auth")
    r = verify.verify(graph, node, diff, original_dir=FIXTURE)
    # путь вне репо не должен попадать в changed
    assert "etc/passwd" not in r["changed"]


# --- CLI JSON-контракт -----------------------------------------------------

def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "mindgraph.cli", *args],
        capture_output=True, text=True)


def test_cli_graph_json_schema_version():
    proc = _run_cli("graph", "--json", "--dir", FIXTURE)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["schema_version"] == "1"
    assert "nodes" in data


def test_cli_boundary_json():
    proc = _run_cli("boundary", "auth", "--json", "--dir", FIXTURE)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["schema_version"] == "1"
    assert "allow_write" in data and "deny_write" in data


def test_cli_verify_returns_exit_3_on_violation():
    diff = ("diff --git a/billing/invoice.py b/billing/invoice.py\n"
            "--- a/billing/invoice.py\n"
            "+++ b/billing/invoice.py\n"
            "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n")
    proc = subprocess.run(
        [sys.executable, "-m", "mindgraph.cli", "verify", "--node", "auth",
         "--diff", "-", "--dir", FIXTURE],
        input=diff, capture_output=True, text=True)
    assert proc.returncode == 3
    data = json.loads(proc.stdout)
    assert data["ok"] is False


def test_cli_bad_args_exit_2():
    proc = _run_cli("node")  # пропущен обязательный id
    assert proc.returncode == 2
