"""Тесты этапа C (shared-слой) на синтетической фикстуре.

owners — по ПРЯМЫМ обратным рёбрам call/uses/inherits (не транзитивно,
без contains, без точек входа). reach — транзитивная версия рядом.
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
    # hash_value зовут issue/refresh (auth) и signature (billing) — прямые
    # владельцы это два кластера. Собственный узел common НЕ входит: в common
    # никто hash_value не зовёт.
    hv = _owners()["common.utils.hash_value"]
    assert hv["owners"] == ["auth", "billing"]
    assert hv["shared"] is True


def test_binding_singleton_is_shared():
    # CACHE (модульный синглтон) читают issue (auth) и signature (billing) —
    # типовая shared-инфраструктура, теперь видимая модели.
    r = _owners()["common.utils.CACHE"]
    assert r["owners"] == ["auth", "billing"]
    assert r["shared"] is True


def test_single_direct_dependent_is_private():
    # normalize зовёт только login (auth) -> один владелец -> приватно.
    # (Транзитивно было бы shared c common — но это связность, не изоляция.)
    r = _owners()["common.utils.normalize"]
    assert r["owners"] == ["auth"]
    assert r["shared"] is False


def test_uncalled_symbol_has_no_owners():
    # refresh никто в репе не зовёт -> ноль владельцев -> приватно.
    r = _owners()["auth.tokens.refresh"]
    assert r["owners"] == []
    assert r["shared"] is False


def test_contains_excluded_from_ownership():
    # signature владеет только через contains (Invoice -> signature), а contains
    # исключён -> у signature ноль владельцев. total же реально зовут (render).
    o = _owners()
    assert o["billing.invoice.Invoice.signature"]["owners"] == []
    assert o["billing.invoice.Invoice.total"]["owners"] == ["billing"]


def test_inherits_and_uses_confer_ownership():
    # Invoice: TaxInvoice наследует + make_invoice использует, оба в billing.
    assert _owners()["billing.invoice.Invoice"]["owners"] == ["billing"]


def test_cycle_terminates_and_is_private():
    # §9.3 — cycle.a <-> cycle.b: прямой расчёт вообще без обхода, тривиально.
    o = _owners()
    assert o["cycle.a.alpha"]["owners"] == ["cycle"]
    assert o["cycle.b.beta"]["owners"] == ["cycle"]


def test_entry_points_excluded_from_owners():
    # Пометим invoice.py точкой входа: make_invoice/TaxInvoice/signature больше
    # не наделяют владением. Тогда Invoice остаётся без владельцев, а hash_value
    # теряет billing (signature был его единственным billing-владельцем).
    o = _owners(entry_points=["invoice.py"])
    assert o["billing.invoice.Invoice"]["owners"] == []
    assert o["common.utils.hash_value"]["owners"] == ["auth"]
    assert o["common.utils.hash_value"]["shared"] is False


def test_reach_is_transitive_and_wider():
    # Транзитивно normalize достижим и из auth, и из своего common.
    g = _graph()
    reach = compute_reach(g["symbols"], g["symbol_edges"])
    assert reach["common.utils.normalize"]["shared"] is True
    assert set(reach["common.utils.normalize"]["owners"]) == {"auth", "common"}


def test_summary_shared_below_reach():
    g = _graph()
    s = summarize(g["symbols"], g["symbol_edges"])
    # знаменатель — реальные символы (без синтетических <module>)
    assert s["symbols"] == 16
    assert s["synthetic"] == 6
    assert set(s["nodes"]) == {"auth", "billing", "common", "cycle"}
    # прямой shared строго уже транзитивного reach
    assert s["shared_count"] == 2          # hash_value + CACHE
    assert s["reach_shared_count"] == 3    # + normalize (транзитивно)
    assert s["shared_ratio"] < s["reach_ratio"]
    assert s["top_shared"][0][0] in ("common.utils.CACHE",
                                     "common.utils.hash_value")
    # длинный хвост: гистограмма покрывает все измеряемые символы
    assert sum(s["owners_hist"].values()) == 16
    assert s["owners_hist"].get(0, 0) > 0   # есть символы без владельцев


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
