"""Stage C tests (shared layer) on the synthetic fixture.

owners — over DIRECT reverse call/uses/inherits edges (not transitively, without
contains, without entry points). reach — the transitive version alongside.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.extract import extract_repo
from mindgraph.shared import (compute_owners, compute_reach, node_of,
                             summarize)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


def _graph():
    return extract_repo(FIXTURE)


def _owners(**kw):
    g = _graph()
    return compute_owners(g["symbols"], g["symbol_edges"], **kw)


def test_node_of():
    assert node_of("app/api/users.py") == "app.api"
    assert node_of("app/models.py") == "app"
    assert node_of("config.py") == "<root>"


def test_shared_util_direct_owners_are_callers_only():
    # hash_value is called by issue/refresh (auth) and signature (billing) — the
    # direct owners are these two clusters. The common node itself does NOT enter:
    # nothing in common calls hash_value.
    hv = _owners()["common.utils.hash_value"]
    assert hv["owners"] == ["auth", "billing"]
    assert hv["shared"] is True


def test_binding_singleton_is_shared():
    # CACHE (a module singleton) is read by issue (auth) and signature (billing) —
    # typical shared infrastructure, now visible to the model.
    r = _owners()["common.utils.CACHE"]
    assert r["owners"] == ["auth", "billing"]
    assert r["shared"] is True


def test_single_direct_dependent_is_private():
    # normalize is called only by login (auth) -> one owner -> private.
    # (Transitively it would be shared with common — but that is connectivity,
    # not isolation.)
    r = _owners()["common.utils.normalize"]
    assert r["owners"] == ["auth"]
    assert r["shared"] is False


def test_uncalled_symbol_has_no_owners():
    # nothing in the repo calls refresh -> zero owners -> private.
    r = _owners()["auth.tokens.refresh"]
    assert r["owners"] == []
    assert r["shared"] is False


def test_contains_excluded_from_ownership():
    # signature is owned only via contains (Invoice -> signature), and contains
    # is excluded -> signature has zero owners. total, on the other hand, is
    # really called (render).
    o = _owners()
    assert o["billing.invoice.Invoice.signature"]["owners"] == []
    assert o["billing.invoice.Invoice.total"]["owners"] == ["billing"]


def test_inherits_and_uses_confer_ownership():
    # Invoice: TaxInvoice inherits + make_invoice uses, both in billing.
    assert _owners()["billing.invoice.Invoice"]["owners"] == ["billing"]


def test_cycle_terminates_and_is_private():
    # §9.3 — cycle.a <-> cycle.b: direct computation with no traversal at all, trivial.
    o = _owners()
    assert o["cycle.a.alpha"]["owners"] == ["cycle"]
    assert o["cycle.b.beta"]["owners"] == ["cycle"]


def test_entry_points_excluded_from_owners():
    # Mark invoice.py an entry point: make_invoice/TaxInvoice/signature no longer
    # confer ownership. Then Invoice is left without owners, and hash_value loses
    # billing (signature was its only billing owner).
    o = _owners(entry_points=["invoice.py"])
    assert o["billing.invoice.Invoice"]["owners"] == []
    assert o["common.utils.hash_value"]["owners"] == ["auth"]
    assert o["common.utils.hash_value"]["shared"] is False


def test_reach_is_transitive_and_wider():
    # Transitively normalize is reachable both from auth and from its own common.
    g = _graph()
    reach = compute_reach(g["symbols"], g["symbol_edges"])
    assert reach["common.utils.normalize"]["shared"] is True
    assert set(reach["common.utils.normalize"]["owners"]) == {"auth", "common"}


def test_summary_shared_below_reach():
    g = _graph()
    s = summarize(g["symbols"], g["symbol_edges"])
    # the denominator — real symbols (without synthetic <module>)
    assert s["symbols"] == 16
    assert s["synthetic"] == 6
    assert set(s["nodes"]) == {"auth", "billing", "common", "cycle"}
    # direct shared is strictly narrower than transitive reach
    assert s["shared_count"] == 2          # hash_value + CACHE
    assert s["reach_shared_count"] == 3    # + normalize (transitively)
    assert s["shared_ratio"] < s["reach_ratio"]
    assert s["top_shared"][0][0] in ("common.utils.CACHE",
                                     "common.utils.hash_value")
    # the long tail: the histogram covers all measured symbols
    assert sum(s["owners_hist"].values()) == 16
    assert s["owners_hist"].get(0, 0) > 0   # there are symbols without owners


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
