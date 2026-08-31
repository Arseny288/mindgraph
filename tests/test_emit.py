"""graph.json build tests (emit.py) on the fixture, everything via --no-llm.

Only package-proxy — the LLM path requires network/key and is covered by
test_group.py at the level of pure functions. Here we verify exactly the §5
contract: the viewer should not need to compute anything itself.
"""

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph import emit

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


def _clean():
    shutil.rmtree(os.path.join(FIXTURE, ".mindgraph"), ignore_errors=True)


def _graph():
    _clean()
    return emit.build_graph(FIXTURE, no_llm=True)


def test_schema_top_level_keys():
    g = _graph()
    assert set(g) == {"version", "grouping", "repo", "symbols", "symbol_edges",
                      "nodes", "node_edges", "domains", "domain_edges",
                      "stats", "warnings"}
    assert g["version"] == 2
    assert g["grouping"] == "package"  # --no-llm without existing LLM layout


def test_repo_meta():
    g = _graph()
    r = g["repo"]
    assert r["name"] == "sample_repo"
    assert r["root"] == os.path.abspath(FIXTURE)
    assert r["repo_hash"].startswith("sha256:")
    assert r["files"] == 6  # 6 parsed .py files (broken.py is skipped)
    assert r["loc"] > 0
    assert r["generated_at"].endswith("Z")


def test_every_symbol_has_node_shared_owners():
    g = _graph()
    for s in g["symbols"]:
        assert "node" in s and s["node"] is not None
        assert "shared" in s and isinstance(s["shared"], bool)
        assert "owners" in s and isinstance(s["owners"], list)
        # the original extract.py fields are also in place — the viewer finds nothing itself
        assert "sig" in s and "lines" in s and "kind" in s


def test_nodes_are_structural_packages_in_no_llm():
    g = _graph()
    names = {n["name"] for n in g["nodes"]}
    assert names == {"auth", "billing", "common", "cycle"}


def test_node_membership_matches_symbol_node_field():
    g = _graph()
    by_node = {n["id"]: set(n["symbols"]) for n in g["nodes"]}
    node_id_of_name = {n["name"]: n["id"] for n in g["nodes"]}
    for s in g["symbols"]:
        assert s["id"] in by_node[node_id_of_name[s["node"]]]


def test_node_loc_is_sum_of_members():
    g = _graph()
    sym_by_id = {s["id"]: s for s in g["symbols"]}
    for n in g["nodes"]:
        assert n["loc"] == sum(sym_by_id[sid]["loc"] for sid in n["symbols"])


def test_node_shared_count_matches_shared_symbols_only_real():
    g = _graph()
    sym_by_id = {s["id"]: s for s in g["symbols"]}
    for n in g["nodes"]:
        expected = sum(1 for sid in n["symbols"]
                       if not sym_by_id[sid].get("synthetic") and sym_by_id[sid]["shared"])
        assert n["shared_count"] == expected


def test_node_edges_aggregate_and_weak_flag():
    g = _graph()
    node_id_of_name = {n["name"]: n["id"] for n in g["nodes"]}
    node_of_symbol = {s["id"]: s["node"] for s in g["symbols"]}

    expected: dict = {}
    for e in g["symbol_edges"]:
        a = node_id_of_name[node_of_symbol[e["from"]]]
        b = node_id_of_name[node_of_symbol[e["to"]]]
        if a == b:
            continue
        expected[(a, b)] = expected.get((a, b), 0) + e["weight"]

    got = {(e["from"], e["to"]): e["weight"] for e in g["node_edges"]}
    assert got == expected
    for e in g["node_edges"]:
        assert e["weak"] == (e["weight"] == 1)

    # no self-edges between nodes (contains/uses within one package must not
    # leak as a node_edge onto itself)
    assert all(e["from"] != e["to"] for e in g["node_edges"])


def test_stats_consistent_with_symbols():
    g = _graph()
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    assert g["stats"]["symbols"] == len(real)
    assert g["stats"]["shared_symbols"] == sum(1 for s in real if s["shared"])
    assert g["stats"]["shared_ratio"] == g["stats"]["shared_symbols"] / g["stats"]["symbols"]
    assert g["stats"]["unassigned"] == 0  # --no-llm: package-proxy leaves nothing unassigned
    assert g["stats"]["external"] == ["os"]


def test_warnings_carry_syntax_error():
    g = _graph()
    assert any("broken.py" in w for w in g["warnings"])


def test_write_graph_round_trips_as_valid_json():
    _clean()
    g = emit.build_graph(FIXTURE, no_llm=True)
    path = emit.write_graph(FIXTURE, g)
    assert path == emit.graph_path(FIXTURE)
    with open(path, encoding="utf-8") as fh:
        reloaded = json.load(fh)
    assert reloaded == g


def test_load_or_build_reuses_cache_on_matching_hash():
    _clean()
    g1, from_cache1 = emit.load_or_build(FIXTURE, no_llm=True)
    assert from_cache1 is False
    g2, from_cache2 = emit.load_or_build(FIXTURE, no_llm=True)
    assert from_cache2 is True
    assert g2["repo"]["repo_hash"] == g1["repo"]["repo_hash"]
    assert g2["repo"]["generated_at"] == g1["repo"]["generated_at"]  # was not recomputed


def test_load_or_build_force_recomputes():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    time.sleep(1.1)  # generated_at — second-level precision
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True, force=True)
    assert from_cache is False
    assert g2["repo"]["generated_at"] != g1["repo"]["generated_at"]


def test_load_or_build_invalidates_on_file_change():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    extra = os.path.join(FIXTURE, "cycle", "_temp_probe.py")
    with open(extra, "w") as fh:
        fh.write("x = 1\n")
    try:
        g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True)
        assert from_cache is False
        assert g2["repo"]["repo_hash"] != g1["repo"]["repo_hash"]
    finally:
        os.remove(extra)


def test_config_fingerprint_present_and_stable():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    assert g1["repo"]["config_fingerprint"].startswith("cfg:")
    g2, _ = emit.load_or_build(FIXTURE, no_llm=True)
    assert g2["repo"]["config_fingerprint"] == g1["repo"]["config_fingerprint"]


def test_model_change_invalidates_cache():
    # 0.3: a model/provider change must NOT silently return the old graph.json.
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True,
                                        model="deepseek-v4-pro")
    assert from_cache is False
    assert g2["repo"]["config_fingerprint"] != g1["repo"]["config_fingerprint"]


def test_provider_change_invalidates_cache():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True, provider_name="deepseek")
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True,
                                        provider_name="anthropic")
    assert from_cache is False
    assert g2["repo"]["config_fingerprint"] != g1["repo"]["config_fingerprint"]


def test_entry_points_change_invalidates_cache():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True,
                                        entry_points=["app.py"])
    assert from_cache is False
    assert g2["repo"]["config_fingerprint"] != g1["repo"]["config_fingerprint"]


def test_node_range_change_invalidates_cache():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True, node_max=8)
    assert from_cache is False
    assert g2["repo"]["config_fingerprint"] != g1["repo"]["config_fingerprint"]


def test_config_fingerprint_embeds_prompt_text():
    # A prompt change (e.g. a wording/translation update) must invalidate the
    # cache even when the code/config are otherwise unchanged.
    f_normal = emit.compute_config_fingerprint("deepseek", None, ["a"], 5, 15, True)
    orig = emit._system_prompt
    emit._system_prompt = lambda *a, **k: "CHANGED PROMPT"
    try:
        f_changed = emit.compute_config_fingerprint("deepseek", None, ["a"], 5, 15, True)
    finally:
        emit._system_prompt = orig
    assert f_normal != f_changed


def test_prompt_change_forces_recalc_of_existing_graph():
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    orig = emit._system_prompt
    emit._system_prompt = lambda *a, **k: "CHANGED PROMPT"
    try:
        g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True)
    finally:
        emit._system_prompt = orig
    assert from_cache is False
    assert g2["repo"]["config_fingerprint"] != g1["repo"]["config_fingerprint"]


def _write_llm_graph():
    """Write a graph.json with a fabricated LLM layout: all symbols into one
    semantic node «Все смыслы» (instead of package-proxy)."""
    _clean()
    base = emit.build_graph(FIXTURE, no_llm=True)
    base["nodes"] = [{
        "id": "n_all", "name": "Все смыслы", "summary": "Тестовый смысловой узел.",
        "symbols": sorted(s["id"] for s in base["symbols"]),
        "files": sorted({s["file"] for s in base["symbols"]}),
        "loc": sum(s["loc"] for s in base["symbols"]),
        "shared_count": 0, "parent": "d_all",
    }]
    base["domains"] = [{"id": "d_all", "name": "Тестовый домен"}]
    for s in base["symbols"]:
        s["node"] = "Все смыслы"
    base["node_edges"] = []
    base["domain_edges"] = []
    base["grouping"] = "llm"
    emit.write_graph(FIXTURE, base)
    return base


def test_no_llm_preserves_existing_llm_nodes():
    # §6: --no-llm must NOT wipe the LLM layout — only recompute the statics.
    _write_llm_graph()
    g, from_cache = emit.load_or_build(FIXTURE, no_llm=True, force=True)
    assert from_cache is False
    assert g["grouping"] == "llm"
    names = {n["name"] for n in g["nodes"]}
    assert "Все смыслы" in names
    assert "auth" not in names  # did not fall back to package-proxy
    auth = next(s for s in g["symbols"] if s["id"] == "auth.login.verify_token")
    assert auth["node"] == "Все смыслы"
    all_node = next(n for n in g["nodes"] if n["name"] == "Все смыслы")
    assert all_node["summary"] == "Тестовый смысловой узел."
    assert all_node["parent"] == "d_all"  # domains are preserved too


def test_force_without_regroup_preserves_llm_nodes():
    # The same scenario as the bug "--force --no-llm wiped 12 nodes": --force by
    # itself is also not a paid regroup — the LLM nodes stay in place.
    _write_llm_graph()
    g, from_cache = emit.load_or_build(FIXTURE, force=True)
    assert from_cache is False
    assert g["grouping"] == "llm"
    assert "Все смыслы" in {n["name"] for n in g["nodes"]}


def test_regroup_bypasses_cache():
    # --regroup — an explicit full recompute, ignores an up-to-date cache.
    _clean()
    g1, _ = emit.load_or_build(FIXTURE, no_llm=True)
    time.sleep(1.1)  # generated_at — second-level precision
    g2, from_cache = emit.load_or_build(FIXTURE, no_llm=True, regroup=True)
    assert from_cache is False
    assert g2["repo"]["generated_at"] != g1["repo"]["generated_at"]


def _run():
    _clean()
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
    _clean()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
