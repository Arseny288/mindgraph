"""Stage B (LLM grouping) tests on the fixture — no network and no keys.

Parsing/validation/bucketing/assignment of pseudo-<module> — pure functions that
do not depend on a particular provider. We test them via FakeProvider, which
returns a pre-set text instead of hitting the API.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.extract import extract_repo
from mindgraph.group import (LLMProvider, UNASSIGNED_NODE, group_repo, node_map,
                            _parse_json_response, _validate_and_bucket,
                            _assign_synthetic_modules, _system_prompt)
from mindgraph.shared import compute_owners_by_node, summarize_by_node

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")

# The layout: authentication + billing are really grouped, common is split
# (hash_value/CACHE — into "Общее", normalize intentionally left without a node),
# cycle.* is also left without a node — three symbols must go to UNASSIGNED_NODE.
FAKE_RESPONSE = {
    "nodes": [
        {"name": "Аутентификация", "summary": "Вход и токены.",
         "symbols": ["auth.tokens.issue", "auth.tokens.refresh",
                    "auth.login.verify_token", "auth.login.login"]},
        {"name": "Биллинг", "summary": "Счета и налоги.",
         "symbols": ["billing.invoice.Invoice", "billing.invoice.Invoice.total",
                    "billing.invoice.Invoice.signature", "billing.invoice.Invoice.render",
                    "billing.invoice.TaxInvoice", "billing.invoice.TaxInvoice.total",
                    "billing.invoice.make_invoice",
                    "nonexistent.symbol"]},  # invalid id — must be dropped
        {"name": "Общее", "summary": "Общая инфраструктура.",
         "symbols": ["common.utils.hash_value", "common.utils.CACHE",
                    "auth.tokens.issue"]},  # duplicate issue — must stay in the first node
    ]
}


class FakeProvider(LLMProvider):
    def __init__(self, text=None, usage=None):
        self.text = text if text is not None else json.dumps(FAKE_RESPONSE, ensure_ascii=False)
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50,
                               "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}
        self.calls = []
        self.completions = []

    def complete(self, system, user, max_tokens=None):
        self.completions.append((system, user))
        return {"text": self.text, "usage": self.usage}

    def group(self, symbols, edges, node_count=None, node_min=5, node_max=15):
        self.calls.append((symbols, edges, node_count))
        return {"text": self.text, "usage": self.usage}


def _graph():
    return extract_repo(FIXTURE)


def test_parse_json_plain():
    parsed, err = _parse_json_response('{"nodes": []}')
    assert err is None
    assert parsed == {"nodes": []}


def test_parse_json_strips_markdown_fence():
    text = "```json\n{\"nodes\": []}\n```"
    parsed, err = _parse_json_response(text)
    assert err is None
    assert parsed == {"nodes": []}


def test_parse_json_repairs_merged_node_objects():
    # Observed in practice (DeepSeek, temperature=0): the model forgets the
    # `}, {` between array elements and glues N nodes into one JSON object with
    # duplicate keys. A plain dict(pairs) would silently keep only the last node
    # ("Биллинг") — the other 8 would vanish without a parse error.
    merged = (
        '{"nodes": [{'
        '"name": "Аутентификация", "summary": "Вход.", "symbols": ["a.login"], '
        '"name": "Биллинг", "summary": "Счета.", "symbols": ["b.invoice"]'
        '}]}'
    )
    parsed, err = _parse_json_response(merged)
    assert err is None
    assert isinstance(parsed["nodes"][0], list)  # the repair returned a list instead of one dict

    warnings = []
    nodes = _validate_and_bucket(parsed, {"a.login", "b.invoice"}, warnings)
    by_name = {n["name"]: n for n in nodes}
    assert by_name["Аутентификация"]["symbols"] == ["a.login"]
    assert by_name["Биллинг"]["symbols"] == ["b.invoice"]
    assert any("glued" in w for w in warnings)


def test_parse_json_does_not_touch_normal_duplicate_free_objects():
    # A normal response without repeated keys passes as before — dict as dict.
    parsed, err = _parse_json_response(
        '{"nodes": [{"name": "X", "summary": "s", "symbols": ["x.y"]}]}')
    assert err is None
    assert isinstance(parsed["nodes"][0], dict)
    assert parsed["nodes"][0]["name"] == "X"


def test_parse_json_unparseable_reports_error():
    parsed, err = _parse_json_response("это не json")
    assert parsed is None
    assert err is not None


def test_validate_and_bucket_drops_invalid_and_dedups():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    warnings = []
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, warnings)

    by_name = {n["name"]: n for n in nodes}
    assert "nonexistent.symbol" not in by_name["Биллинг"]["symbols"]
    # the duplicated issue stays only in the first node where it was seen
    assert "auth.tokens.issue" in by_name["Аутентификация"]["symbols"]
    assert "auth.tokens.issue" not in by_name["Общее"]["symbols"]
    assert any("nonexistent.symbol" in w for w in warnings)
    assert any("issue" in w and "more than one" in w for w in warnings)


def test_validate_and_bucket_unassigned_goes_to_unassigned_node():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, [])
    by_name = {n["name"]: n for n in nodes}
    assert set(by_name[UNASSIGNED_NODE]["symbols"]) == {
        "common.utils.normalize", "cycle.a.alpha", "cycle.b.beta",
    }


def test_assign_synthetic_modules_majority_vote():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, [])
    _assign_synthetic_modules(nodes, g["symbols"])
    m = node_map(nodes)

    assert m["auth.tokens.<module>"] == "Аутентификация"
    assert m["auth.login.<module>"] == "Аутентификация"
    assert m["billing.invoice.<module>"] == "Биллинг"
    # common.utils.<module>: hash_value+CACHE (Общее, 2) > normalize (Unassigned, 1)
    assert m["common.utils.<module>"] == "Общее"
    # cycle.*.<module>: the file's only real symbol went to Unassigned
    assert m["cycle.a.<module>"] == UNASSIGNED_NODE
    assert m["cycle.b.<module>"] == UNASSIGNED_NODE


def test_system_prompt_respects_node_range():
    assert "5-15" in _system_prompt()
    assert "3-8" in _system_prompt(None, 3, 8)
    assert "exactly 6" in _system_prompt(None, 6, 6)
    assert "exactly 6" in _system_prompt(6)  # exact K beats the range


def test_group_repo_threads_node_range_to_provider():
    g = _graph()
    seen = {}

    class P(FakeProvider):
        def group(self, symbols, edges, node_count=None, node_min=5, node_max=15):
            seen["range"] = (node_min, node_max)
            return super().group(symbols, edges, node_count, node_min, node_max)

    group_repo(g["symbols"], g["symbol_edges"], P(), node_min=3, node_max=8)
    assert seen["range"] == (3, 8)


def test_group_repo_end_to_end_with_fake_provider():
    g = _graph()
    provider = FakeProvider()
    result = group_repo(g["symbols"], g["symbol_edges"], provider)

    assert result["unassigned_count"] == 3
    assert result["usage"]["input_tokens"] == 100
    assert len(provider.calls) == 1

    m = node_map(result["nodes"])
    # every symbol (real and synthetic) got exactly one node
    all_ids = {s["id"] for s in g["symbols"]}
    assert set(m) == all_ids


def test_group_repo_unparseable_response_falls_back_to_all_unassigned():
    g = _graph()
    provider = FakeProvider(text="не json вообще")
    result = group_repo(g["symbols"], g["symbol_edges"], provider)

    assert any("parse" in w for w in result["warnings"])
    by_name = {n["name"]: n for n in result["nodes"]}
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    # real symbols go to Unassigned directly; the grouping quality counter is
    # counted BEFORE assigning the synthetic <module>...
    assert result["unassigned_count"] == len(real_ids)
    # ...but since all real symbols of every file also went to Unassigned, by
    # majority vote the <module> go there too — the whole graph in one node.
    assert set(by_name[UNASSIGNED_NODE]["symbols"]) == {s["id"] for s in g["symbols"]}


def test_semantic_shared_ratio_via_node_map():
    # Общее holds both hash_value and CACHE, but their real callers (issue,
    # signature) sit in different nodes (Аутентификация/Биллинг) -> shared.
    g = _graph()
    nodes = _validate_and_bucket(FAKE_RESPONSE,
                                 {s["id"] for s in g["symbols"] if not s.get("synthetic")}, [])
    _assign_synthetic_modules(nodes, g["symbols"])
    m = node_map(nodes)

    s = summarize_by_node(g["symbols"], g["symbol_edges"], m)
    assert s["shared_ratio"] > 0
    hash_value_owners = s["owners"]["common.utils.hash_value"]["owners"]
    assert set(hash_value_owners) == {"Аутентификация", "Биллинг"}


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
