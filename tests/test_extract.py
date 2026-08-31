"""Stage A tests on the synthetic fixture.

The symbol and edge counts are hardcoded to exact values (see §9.1 of the spec):
the fixture has a known structure, any divergence is a regression.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.extract import extract_repo

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


def _graph():
    return extract_repo(FIXTURE)


def _edges(etype):
    return {(e["from"], e["to"]): e["weight"]
            for e in _graph()["symbol_edges"] if e["type"] == etype}


def test_symbol_count():
    g = _graph()
    assert len(g["symbols"]) == 22
    kinds = {}
    for s in g["symbols"]:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    assert kinds == {"function": 9, "method": 4, "class": 2,
                     "binding": 1, "module": 6}


def test_symbol_ids():
    ids = {s["id"] for s in _graph()["symbols"]}
    assert ids == {
        "common.utils.hash_value", "common.utils.normalize",
        "common.utils.CACHE",
        "auth.tokens.issue", "auth.tokens.refresh",
        "auth.login.verify_token", "auth.login.login",
        "billing.invoice.Invoice", "billing.invoice.Invoice.total",
        "billing.invoice.Invoice.signature", "billing.invoice.Invoice.render",
        "billing.invoice.TaxInvoice", "billing.invoice.TaxInvoice.total",
        "billing.invoice.make_invoice",
        "cycle.a.alpha", "cycle.b.beta",
        # pseudo-<module> for every parsed file
        "common.utils.<module>", "auth.tokens.<module>", "auth.login.<module>",
        "billing.invoice.<module>", "cycle.a.<module>", "cycle.b.<module>",
    }


def test_bindings_extracted():
    by_id = {s["id"]: s for s in _graph()["symbols"]}
    cache = by_id["common.utils.CACHE"]
    assert cache["kind"] == "binding"
    assert cache["sig"] == "CACHE = {}"
    assert cache["synthetic"] is False


def test_module_pseudosymbols():
    by_id = {s["id"]: s for s in _graph()["symbols"]}
    mod = by_id["cycle.a.<module>"]
    assert mod["kind"] == "module"
    assert mod["synthetic"] is True
    assert mod["file"] == "cycle/a.py"


def test_symbol_fields():
    by_id = {s["id"]: s for s in _graph()["symbols"]}
    issue = by_id["auth.tokens.issue"]
    assert issue["sig"] == "issue(user_id: int) -> str"
    assert issue["doc"] == "Выдаёт токен для пользователя."
    assert issue["file"] == "auth/tokens.py"
    assert by_id["common.utils.normalize"]["doc"] is None
    assert by_id["billing.invoice.Invoice"]["sig"] == "class Invoice"
    assert by_id["billing.invoice.TaxInvoice"]["sig"] == "class TaxInvoice(Invoice)"


def test_edge_type_distribution():
    edges = _graph()["symbol_edges"]
    dist = {}
    for e in edges:
        dist[e["type"]] = dist.get(e["type"], 0) + 1
    # call: +cycle.a.<module>->alpha; uses: issue/signature->CACHE + make_invoice
    assert dist == {"call": 9, "uses": 3, "contains": 4, "inherits": 1}
    assert len(edges) == 17


def test_binding_references_resolve():
    # `from common.utils import CACHE` + CACHE[...] -> a uses edge onto the binding
    uses = _edges("uses")
    assert ("auth.tokens.issue", "common.utils.CACHE") in uses
    assert ("billing.invoice.Invoice.signature", "common.utils.CACHE") in uses


def test_module_pseudosymbol_participates_in_edges():
    # the top-level alpha() call is attributed to the pseudo-<module>
    calls = _edges("call")
    assert ("cycle.a.<module>", "cycle.a.alpha") in calls


def test_no_fanout_only_real_mentions():
    # verify_token imports issue (via login? no — login.py itself), but does NOT
    # mention anything in-repo in its body -> zero outgoing edges.
    # This is exactly what distinguishes uses from the old fan-out import.
    out = {(e["from"], e["to"], e["type"])
           for e in _graph()["symbol_edges"]
           if e["from"] == "auth.login.verify_token"}
    assert out == set()


def test_call_absorbs_uses_after_dedup():
    uses = _edges("uses")
    calls = _edges("call")
    # for called pairs only call remains, uses is dropped by dedup
    for pair in [
        ("auth.tokens.issue", "common.utils.hash_value"),
        ("auth.login.login", "common.utils.normalize"),   # module.attr
        ("billing.invoice.Invoice.render",
         "billing.invoice.Invoice.total"),                # self.foo()
    ]:
        assert pair in calls
        assert pair not in uses


def test_uses_without_call_and_weight():
    uses = _edges("uses")
    calls = _edges("call")
    key = ("billing.invoice.make_invoice", "billing.invoice.Invoice")
    # make_invoice mentions Invoice twice (annotation + assignment),
    # but does not call it directly -> uses with weight 2, no call.
    assert uses[key] == 2
    assert key not in calls


def test_inherits():
    inh = _edges("inherits")
    assert inh == {("billing.invoice.TaxInvoice", "billing.invoice.Invoice"): 1}


def test_contains():
    contains = set(_edges("contains"))
    assert contains == {
        ("billing.invoice.Invoice", "billing.invoice.Invoice.total"),
        ("billing.invoice.Invoice", "billing.invoice.Invoice.signature"),
        ("billing.invoice.Invoice", "billing.invoice.Invoice.render"),
        ("billing.invoice.TaxInvoice", "billing.invoice.TaxInvoice.total"),
    }


def test_incoming_to_invoice():
    incoming = {(e["from"], e["type"])
                for e in _graph()["symbol_edges"]
                if e["to"] == "billing.invoice.Invoice"}
    assert incoming == {
        ("billing.invoice.TaxInvoice", "inherits"),
        ("billing.invoice.make_invoice", "uses"),
    }


def test_relative_import_resolution():
    # `from .tokens import issue` resolves via the file's position in the tree.
    calls = _edges("call")
    assert ("auth.login.login", "auth.tokens.issue") in calls


def test_cycle_does_not_break_and_is_bidirectional():
    calls = _edges("call")
    uses = _edges("uses")
    assert ("cycle.a.alpha", "cycle.b.beta") in calls
    assert ("cycle.b.beta", "cycle.a.alpha") in calls
    # both pairs are called -> uses is dropped by dedup
    assert ("cycle.a.alpha", "cycle.b.beta") not in uses
    assert ("cycle.b.beta", "cycle.a.alpha") not in uses


def test_shared_util_targeted_by_both_clusters():
    edges = _graph()["symbol_edges"]
    sources = {e["from"] for e in edges
               if e["to"] == "common.utils.hash_value"}
    assert any(s.startswith("auth.") for s in sources)
    assert any(s.startswith("billing.") for s in sources)


def test_no_self_edges():
    for e in _graph()["symbol_edges"]:
        assert e["from"] != e["to"]


def test_syntax_error_warning_and_others_survive():
    g = _graph()
    assert len(g["warnings"]) == 1
    assert "broken.py" in g["warnings"][0]
    assert "syntax error" in g["warnings"][0]
    assert len(g["symbols"]) == 22


def test_external_packages():
    assert _graph()["external"] == ["os"]


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
