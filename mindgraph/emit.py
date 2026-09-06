"""Assembly and writing of graph.json (§5 of the spec) — the only contract with the viewer.

The viewer knows nothing about Python and computes nothing itself: shared/owners
by symbol, aggregated edges between nodes with weights and `weak`, all stats —
everything is already computed here. extract.py/group.py/shared.py remain
read-only analyzers; emit.py orchestrates them and decides what to write to disk
and where.

Cache: if the project's ``graph.json`` (see :mod:`mindgraph.store` — the
storage amendment: nothing is written into the analyzed repo) already exists
and its ``repo.repo_hash`` matches the current one — we reuse it without
recomputing (unless ``--force``). ``repo_hash`` is a sha256 over (path, size,
mtime) of the scanned .py files, not over their content: it is cheap, and as
a "has not changed" signal it suffices.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone

from .domains import assign_domains, UNASSIGNED_DOMAIN, _DOMAIN_SYSTEM_PROMPT
from .extract import extract_repo, _iter_python_files, _iter_ignore_files, _rel
from .group import (group_repo, make_provider, node_map, UNASSIGNED_NODE,
                    _system_prompt, DEEPSEEK_MODEL, ANTHROPIC_MODEL)
from . import manifest
from . import store
from .shared import node_of, compute_owners_by_node, ENTRY_POINT_PATTERNS, _is_entry_point

log = logging.getLogger("mindgraph.emit")

GRAPH_VERSION = 2  # +domains, +domain_edges, +node.parent, +grouping (additive, §5 of the spec)


def graph_path(root: str) -> str:
    """The built graph's path — under the tool's own storage root (see
    ``store.py``), never inside the analyzed repo."""
    return store.resolve_project(root).graph_path


def _runs_dir(root: str) -> str:
    return os.path.join(store.resolve_project(root).root, "runs")


def _resolve_model(provider_name: str, model: str | None) -> str:
    """The effective model of the provider (explicit or the provider's default)."""
    if model:
        return model
    return DEEPSEEK_MODEL if provider_name == "deepseek" else ANTHROPIC_MODEL


def compute_config_fingerprint(provider_name: str, model: str | None,
                               entry_points, node_min: int, node_max: int,
                               no_llm: bool) -> str:
    """Hash of the effective config that affects the graph.json build result.

    ``repo_hash`` covers ONLY the sources (what we read); this hash covers HOW we
    read (provider, model, prompt versions, entry_points, node range, the
    extractor/schema version). Changing any of them must invalidate the cache on
    par with a code edit: an old graph.json with the old grouping and owners
    cannot be silently reused.

    Prompt versions are not taken as a constant but as a hash of the REAL prompt
    text — editing the system prompt in group.py/domains.py changes the
    fingerprint itself, without manual counter synchronization.
    """
    grouping_prompt = _system_prompt(None, node_min, node_max)
    h = hashlib.sha256()
    for part in (
        provider_name,
        _resolve_model(provider_name, model),
        "\n".join(entry_points),
        str(node_min),
        str(node_max),
        "no-llm" if no_llm else "llm",
        grouping_prompt,
        _DOMAIN_SYSTEM_PROMPT,
        f"schema:{GRAPH_VERSION}",
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return "cfg:" + h.hexdigest()[:24]


def compute_repo_hash(root: str, extra_ignore=()) -> str:
    """sha256((relpath, size, mtime) over .py files + the content of ignore files).

    Ignore files (.gitignore/.mindgraphignore) are included by content: editing
    the exclusion rules invalidates the cache on par with a code edit.
    """
    h = hashlib.sha256()
    for path in _iter_python_files(root, extra_ignore):
        rel = _rel(root, path)
        st = os.stat(path)
        h.update(f"{rel}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    for path, rel in _iter_ignore_files(root, extra_ignore):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            content = ""
        h.update(f"ignore:{rel}:{content}\n".encode())
    return "sha256:" + h.hexdigest()


def _repo_loc(root: str, extra_ignore=()) -> int:
    total = 0
    for path in _iter_python_files(root, extra_ignore):
        with open(path, encoding="utf-8", errors="replace") as fh:
            total += sum(1 for _ in fh)
    return total


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", name.strip().lower()).strip("_")
    return s or "node"


def _save_run(root: str, provider_name: str, node_count, result: dict) -> str:
    """The raw model response in the project's ``runs/`` dir — grouping audit/debug:
    if the model sent invalid JSON or the layout "drifted", we can see exactly
    what it returned, without a repeated paid call.
    """
    runs_dir = _runs_dir(root)
    os.makedirs(runs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    k = "auto" if node_count is None else str(node_count)
    path = os.path.join(runs_dir, f"{ts}_{provider_name}_k{k}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "provider": provider_name,
            "node_count": node_count,
            "raw_text": result["raw_text"],
            "usage": result["usage"],
            "warnings": result["warnings"],
            "unassigned_count": result["unassigned_count"],
        }, fh, ensure_ascii=False, indent=2)
    return path


def _save_domain_run(root: str, provider_name: str, result: dict) -> str:
    """Like :func:`_save_run`, but for the second (domain) request — its own
    directory, so as not to mix up the raw responses of two different prompts."""
    runs_dir = _runs_dir(root)
    os.makedirs(runs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    path = os.path.join(runs_dir, f"{ts}_{provider_name}_domains.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "provider": provider_name,
            "raw_text": result["raw_text"],
            "usage": result["usage"],
            "warnings": result["warnings"],
            "domains": result["domains"],
        }, fh, ensure_ascii=False, indent=2)
    return path


def _has_llm_grouping(graph: dict) -> bool:
    """Whether ``graph.json`` has nodes from the model (rather than package-proxy).

    The explicit ``grouping`` field is the marker written since this version. For
    a graph.json without it (version ≤2) — a heuristic: package-proxy leaves node
    ``summary`` fields empty, LLM grouping fills them in.
    """
    if graph.get("grouping") == "llm":
        return True
    if graph.get("grouping") == "package":
        return False
    return any(n.get("summary") for n in graph.get("nodes", []))


def _preserved_grouping(graph: dict):
    """Extract the LLM layout of an existing graph.json for transfer under --no-llm.

    Returns ``(node_of_symbol, node_summary, node_parent, domains)`` — exactly
    what must be re-saved so that a static recompute does not wipe the model's
    semantic nodes (see :func:`build_graph`, the ``preserve_from`` parameter).
    """
    symbol_node = {}
    for s in graph.get("symbols", []):
        if s.get("node"):
            symbol_node[s["id"]] = s["node"]
    node_summary = {}
    node_parent = {}
    for n in graph.get("nodes", []):
        node_summary[n["name"]] = n.get("summary", "")
        if n.get("parent"):
            node_parent[n["name"]] = n["parent"]
    return symbol_node, node_summary, node_parent, list(graph.get("domains", []))


def _restore_domains(out_nodes: list, preserved_parent: dict,
                     preserved_domains: list) -> list:
    """Restore domains, keeping the previous ``parent`` of existing L1 nodes.

    New nodes (symbols that appeared after a code change) go to the service
    domain :data:`UNASSIGNED_DOMAIN` until the next ``--regroup``.
    """
    valid = {d["id"] for d in preserved_domains}
    fallback_id = "d_" + _slug(UNASSIGNED_DOMAIN)
    need_fallback = False
    for n in out_nodes:
        pid = preserved_parent.get(n["name"])
        if pid and pid in valid:
            n["parent"] = pid
        else:
            n["parent"] = fallback_id
            need_fallback = True
    domains = list(preserved_domains)
    if need_fallback:
        domains.append({"id": fallback_id, "name": UNASSIGNED_DOMAIN})
    return domains


def build_graph(root: str, provider_name: str = "deepseek", no_llm: bool = False,
                repo_hash: str | None = None,
                preserve_from: dict | None = None,
                extra_ignore=(),
                model: str | None = None,
                cache_base=None,
                entry_points=None,
                node_min: int = 5, node_max: int = 15,
                config_fingerprint: str | None = None,
                generate_descriptions: bool = False,
                existing_for_descriptions: dict | None = None,
                regenerate_descriptions: bool = False) -> dict:
    """Extract -> (group) -> shared -> a single graph.json (in memory, without writing).

    If the LLM provider is unavailable (no key/package) and ``no_llm`` is not set
    explicitly — we silently fall back to package-proxy (see shared.py) and write
    a warning to graph.warnings instead of crashing (§1 of the spec: never crash
    wholesale).

    ``preserve_from`` — an existing graph.json with LLM layout. When set, no LLM
    call is made at all: the statics (symbols/edges/shared/stats) are recomputed
    from scratch, while the assignment of symbols to nodes, summaries and domains
    are taken from it. This way ``--no-llm``/an ordinary recompute never wipes
    the LLM layout; a full regrouping is only a separate ``--regroup``.

    ``model`` — an override of the provider's default model; ``cache_base`` — the
    LLM-response cache directory (see llmcache.py); ``entry_points`` — glob
    patterns of entry points (default ENTRY_POINT_PATTERNS);
    ``config_fingerprint`` — the effective config hash (see
    compute_config_fingerprint), written into ``repo`` and checked when loading
    the cache (otherwise a provider/model change would silently return the old
    grouping).
    """
    root = os.path.abspath(root)
    log.info("stage A: static analysis")
    extracted = extract_repo(root, extra_ignore)
    symbols, symbol_edges = extracted["symbols"], extracted["symbol_edges"]
    warnings = list(extracted["warnings"])
    log.info("stage A: %d symbols, %d edges", len(symbols), len(symbol_edges))
    entry_patterns = ENTRY_POINT_PATTERNS if entry_points is None else entry_points
    if config_fingerprint is None:
        config_fingerprint = compute_config_fingerprint(
            provider_name, model, entry_patterns, node_min, node_max, no_llm)

    node_of_symbol: dict = {}
    node_meta: dict = {}
    node_id_override: dict = {}  # node name -> stable id (from the manifest)
    unassigned = 0
    grouping = "package"
    preserved_parent: dict = {}
    preserved_domains: list = []

    if preserve_from is not None:
        log.info("stage B: transferring the existing LLM layout (no model call)")
        preserved_node, preserved_summary, preserved_parent, preserved_domains = \
            _preserved_grouping(preserve_from)
        # Deterministic additions on top of the preserved layout (README
        # cache-first; STUDIO.md §10): the manifest's own_paths win mechanically,
        # a symbol new since the last scan joins the node that owns its file, and
        # everything still unclaimed lands in the "Unassigned" service node —
        # new code never silently falls back to package-proxy names.
        manifest_nodes = manifest.load(root)
        pinned_sym = manifest.preassign(
            [s for s in symbols if not s.get("synthetic")], manifest_nodes) \
            if manifest_nodes else {}
        old_id_name = {n.get("id"): n.get("name") for n in (preserve_from.get("nodes") or [])}
        mn_by_id = {n["id"]: n for n in manifest_nodes}

        def _manifest_node_name(nid):
            """The graph name of a manifest node: its old name if the node was
            already in the graph (identity by id), else its title."""
            mn = mn_by_id.get(nid)
            if mn is None:
                return None
            return old_id_name.get(nid) or mn["title"]

        # The owning node of each file among symbols that WERE preserved.
        file_owners: dict = {}
        for s in symbols:
            if s["id"] in preserved_node:
                file_owners.setdefault(s["file"], []).append(preserved_node[s["id"]])

        # Pass 1 — real symbols (the manifest preassigns real symbols only, and
        # synthetic <module> pseudo-symbols must follow their file's real owner).
        file_real_count: dict = {}  # file -> Counter of decided node names
        for s in symbols:
            sid = s["id"]
            if s.get("synthetic"):
                continue
            pinned = pinned_sym.get(sid)
            if pinned is not None:
                name = _manifest_node_name(pinned)
                if name is not None:
                    node_of_symbol[sid] = name
                    file_real_count.setdefault(s["file"], Counter())[name] += 1
                    continue
            if sid in preserved_node:
                node_of_symbol[sid] = preserved_node[sid]
                file_real_count.setdefault(s["file"], Counter())[preserved_node[sid]] += 1
                continue
            # A real symbol new since the last scan.
            majority = Counter(file_owners.get(s["file"], [])).most_common(1)
            name = majority[0][0] if majority else UNASSIGNED_NODE
            node_of_symbol[sid] = name
            file_real_count.setdefault(s["file"], Counter())[name] += 1

        # Pass 2 — synthetic <module> pseudo-symbols not in the old graph: join
        # the node their file's real symbols landed in, else "Unassigned".
        for s in symbols:
            sid = s["id"]
            if not s.get("synthetic"):
                continue
            if sid in preserved_node:
                node_of_symbol[sid] = preserved_node[sid]
                continue
            majority = (file_real_count.get(s["file"]) or Counter()).most_common(1)
            node_of_symbol[sid] = majority[0][0] if majority else UNASSIGNED_NODE
        node_meta = {name: preserved_summary.get(name, "")
                     for name in set(node_of_symbol.values())}
        unassigned = sum(1 for s in symbols
                         if not s.get("synthetic")
                         and node_of_symbol[s["id"]] == UNASSIGNED_NODE)
        grouping = "llm"
    elif not no_llm:
        log.info("stage B: grouping (%s)", provider_name)
        try:
            provider = make_provider(provider_name) if model is None \
                else make_provider(provider_name, model=model)
        except Exception as e:  # no key, no package, etc. — not fatal
            warnings.append(
                f"LLM provider {provider_name!r} is unavailable ({e}); running as --no-llm")
            provider = None
        if provider is not None:
            manifest_nodes = manifest.load(root)
            if manifest_nodes:
                real = [s for s in symbols if not s.get("synthetic")]
                pinned = manifest.preassign(real, manifest_nodes)
                result = group_repo(
                    symbols, symbol_edges, provider, cache_base=cache_base,
                    node_min=node_min, node_max=node_max, pinned=pinned,
                    existing_nodes=[{"id": n["id"], "title": n["title"]}
                                    for n in manifest_nodes])
                # Stable ids come from the result's nodes (the title may have
                # been refined by the model, the id — not).
                for n in result["nodes"]:
                    if "id" in n:
                        node_id_override[n["name"]] = n["id"]
            else:
                result = group_repo(symbols, symbol_edges, provider,
                                    cache_base=cache_base,
                                    node_min=node_min, node_max=node_max)
            _save_run(root, provider_name, None, result)
            warnings.extend(result["warnings"])
            node_of_symbol = node_map(result["nodes"])
            node_meta = {n["name"]: n.get("summary", "") for n in result["nodes"]}
            unassigned = result["unassigned_count"]
            grouping = "llm"

    if not node_of_symbol:  # --no-llm or the provider is unavailable
        node_of_symbol = {s["id"]: node_of(s["file"], "package") for s in symbols}
        node_meta = {name: "" for name in set(node_of_symbol.values())}
        grouping = "package"

    entry_ids = {s["id"] for s in symbols if _is_entry_point(s["file"], entry_patterns)}
    direct = compute_owners_by_node(symbols, symbol_edges, node_of_symbol, entry_ids=entry_ids)

    out_symbols = []
    for s in symbols:
        o = direct.get(s["id"], {"node": node_of_symbol.get(s["id"]), "owners": [], "shared": False})
        entry = dict(s)
        entry.update({"node": o["node"], "shared": o["shared"], "owners": o["owners"]})
        out_symbols.append(entry)

    measured = [s for s in symbols if not s.get("synthetic")]
    shared_symbols = [s for s in measured if direct[s["id"]]["shared"]]
    shared_ratio = (len(shared_symbols) / len(measured)) if measured else 0.0

    by_node: dict = {}
    for s in symbols:
        by_node.setdefault(node_of_symbol[s["id"]], []).append(s)

    out_nodes = []
    for name, members in sorted(by_node.items()):
        nid = node_id_override.get(name) or ("n_" + _slug(name))
        shared_count = sum(1 for m in members
                           if not m.get("synthetic") and direct[m["id"]]["shared"])
        out_nodes.append({
            "id": nid, "name": name, "summary": node_meta.get(name, ""),
            "symbols": sorted(m["id"] for m in members),
            "files": sorted({m["file"] for m in members}),
            "loc": sum(m["loc"] for m in members),
            "shared_count": shared_count,
        })
    node_id_of_name = {n["name"]: n["id"] for n in out_nodes}

    agg: dict = {}
    for e in symbol_edges:
        a = node_id_of_name.get(node_of_symbol.get(e["from"]))
        b = node_id_of_name.get(node_of_symbol.get(e["to"]))
        if a is None or b is None or a == b:
            continue
        agg[(a, b)] = agg.get((a, b), 0) + e["weight"]
    out_node_edges = [{"from": a, "to": b, "weight": w, "weak": w == 1}
                      for (a, b), w in sorted(agg.items())]

    # Stage B2 (domains.py) — a SEPARATE second request to the model, grouping the
    # already-ready L1 nodes into L0 domains. Symbols are not passed there at all,
    # and assign_domains must not touch them — the assert below guarantees this:
    # if the symbol-to-L1-node layout diverged by even one id before/after the
    # call, this graph.json build must fail rather than silently diverge from
    # what was already verified at stage B (§3 of the spec).
    if preserve_from is not None:
        domains = _restore_domains(out_nodes, preserved_parent, preserved_domains)
    else:
        log.info("stage B2: domains")
        before_symbols = {n["id"]: set(n["symbols"]) for n in out_nodes}
        domains_result = assign_domains(out_nodes, out_node_edges,
                                        provider_name=provider_name, no_llm=no_llm,
                                        cache_base=cache_base, model=model)
        if domains_result["raw_text"]:
            _save_domain_run(root, provider_name, domains_result)
        warnings.extend(domains_result["warnings"])
        after_symbols = {n["id"]: set(n["symbols"]) for n in out_nodes}
        assert before_symbols == after_symbols, (
            "assign_domains changed the symbol set of at least one L1 node — "
            "invariant violated (domains must not touch symbol assignment)")
        domains = domains_result["domains"]

    domain_id_of_node_id = {n["id"]: n.get("parent") for n in out_nodes}
    dagg: dict = {}
    for e in out_node_edges:
        da, db = domain_id_of_node_id.get(e["from"]), domain_id_of_node_id.get(e["to"])
        if da is None or db is None or da == db:
            continue
        dagg[(da, db)] = dagg.get((da, db), 0) + e["weight"]
    out_domain_edges = [{"from": a, "to": b, "weight": w, "weak": w == 1}
                        for (a, b), w in sorted(dagg.items())]

    graph = {
        "version": GRAPH_VERSION,
        "grouping": grouping,
        "repo": {
            "root": root,
            "name": os.path.basename(root.rstrip("/")) or root,
            "repo_hash": repo_hash or compute_repo_hash(root, extra_ignore),
            "config_fingerprint": config_fingerprint,
            "files": len({s["file"] for s in symbols}),
            "loc": _repo_loc(root, extra_ignore),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "symbols": sorted(out_symbols, key=lambda s: s["id"]),
        "symbol_edges": symbol_edges,
        "nodes": out_nodes,
        "node_edges": out_node_edges,
        "domains": domains,
        "domain_edges": out_domain_edges,
        "stats": {
            "symbols": len(measured),
            "unassigned": unassigned,
            "shared_symbols": len(shared_symbols),
            "shared_ratio": shared_ratio,
            "external": extracted["external"],
        },
        "warnings": warnings,
    }

    # --- descriptions (item 7): one batched pass, after the node breakdown
    # and dependency graph above are final. Never a build failure — a node
    # that can't be described gets description: None and the run continues.
    # Opt-in (generate_descriptions): direct build_graph() callers — mainly
    # the test suite, always with no_llm=True — default to off, so adding
    # this never makes an existing test hit the network. load_or_build turns
    # it on for the real CLI/studio path (tied to the caller's own no_llm,
    # not the no_llm this function may have been called with internally to
    # skip re-grouping while preserving an existing LLM layout).
    log.info("stage C: descriptions")
    graph["stats"]["descriptions"] = _run_descriptions(
        root, graph, provider_name=provider_name, model=model,
        enabled=generate_descriptions,
        existing_graph=existing_for_descriptions, force=regenerate_descriptions)

    return graph


def _run_descriptions(root: str, graph: dict, provider_name: str, model: str | None,
                      enabled: bool, existing_graph: dict | None, force: bool) -> dict:
    """Every node/domain always gets the four description_* keys — None/empty
    when descriptions are off or the provider is unavailable, so the schema
    is the same either way. See describe.py for the actual generation."""
    total = len(graph.get("nodes", [])) + len(graph.get("domains", []))
    empty_summary = {"total": total, "described": 0, "failed": [], "reused": 0,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}

    def mark_ungenerated():
        for n in graph["nodes"]:
            n.setdefault("description", None)
            n.setdefault("description_hash", "")
            n.setdefault("description_model", "")
            n.setdefault("description_generated_at", "")
        for d in graph["domains"]:
            d.setdefault("description", None)
            d.setdefault("description_hash", "")
            d.setdefault("description_model", "")
            d.setdefault("description_generated_at", "")

    if not enabled:
        mark_ungenerated()
        return empty_summary
    from . import describe
    try:
        provider = make_provider(provider_name) if model is None \
            else make_provider(provider_name, model=model)
    except Exception as e:
        log.warning("descriptions skipped: provider %r unavailable (%s)", provider_name, e)
        mark_ungenerated()
        return empty_summary
    return describe.build_descriptions(
        root, graph, provider, existing_graph=existing_graph, force=force,
        model_name=_resolve_model(provider_name, model))


def write_graph(root: str, graph: dict) -> str:
    path = graph_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=2)
    return path


def load_or_build(root: str, provider_name: str = "deepseek", no_llm: bool = False,
                  force: bool = False, regroup: bool = False, extra_ignore=(),
                  model: str | None = None, cache_base=None, entry_points=None,
                  node_min: int = 5, node_max: int = 15,
                  regenerate_descriptions: bool = False):
    """Return ``(graph, from_cache: bool)``. Writes to disk on recompute.

    Cache-first: if ``repo_hash`` AND ``config_fingerprint`` in the project's
    ``graph.json`` (see :func:`graph_path`) match the current ones and there is no
    ``--force``/``--regroup``/``--regenerate-descriptions`` — return the cache
    without recomputing. LLM-layout protection (§6): ``--no-llm`` (and an
    ordinary recompute on a changed hash without ``--regroup``) recomputes
    only the statics, leaving the existing model nodes in place. A full
    recompute — only ``--regroup`` (and it is free on unchanged code — the
    LLM-response cache is keyed by prompt hash).

    Descriptions (item 7) regenerate only for nodes/domains whose
    ``description_hash`` changed since ``existing`` — passed through as
    ``existing_for_descriptions`` regardless of which build_graph branch
    below runs, so a plain recompute (not just --regroup) still reuses them.
    ``generate_descriptions`` follows THIS call's own ``no_llm`` — not
    whatever build_graph is internally called with to skip re-grouping.
    """
    root = os.path.abspath(root)
    current_hash = compute_repo_hash(root, extra_ignore)
    resolved_ep = ENTRY_POINT_PATTERNS if entry_points is None else entry_points
    current_fingerprint = compute_config_fingerprint(
        provider_name, model, resolved_ep, node_min, node_max, no_llm)
    path = graph_path(root)

    existing = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = None  # damaged cache — recompute silently

    generate_descriptions = not no_llm

    # --regroup: a full recompute of the LLM layout, always, even with an
    # up-to-date cache (that is exactly the flag's meaning — explicit regrouping).
    if regroup:
        graph = build_graph(root, provider_name=provider_name, no_llm=False,
                            repo_hash=current_hash, extra_ignore=extra_ignore,
                            model=model, cache_base=cache_base,
                            entry_points=entry_points,
                            node_min=node_min, node_max=node_max,
                            config_fingerprint=current_fingerprint,
                            generate_descriptions=generate_descriptions,
                            existing_for_descriptions=existing,
                            regenerate_descriptions=regenerate_descriptions)
        write_graph(root, graph)
        return graph, False

    # The cache is up to date (code AND config unchanged) and neither --force
    # nor --regenerate-descriptions is set.
    if not force and not regenerate_descriptions and existing is not None \
            and existing.get("repo", {}).get("repo_hash") == current_hash \
            and existing.get("repo", {}).get("config_fingerprint") == current_fingerprint:
        return existing, True

    # Recompute the statics. If graph.json already has LLM nodes — transfer them
    # (never wipe); a full regrouping — only via --regroup.
    preserve_from = existing if (existing is not None and _has_llm_grouping(existing)) else None

    if preserve_from is not None:
        graph = build_graph(root, provider_name=provider_name, no_llm=True,
                            repo_hash=current_hash, preserve_from=preserve_from,
                            extra_ignore=extra_ignore, model=model,
                            cache_base=cache_base, entry_points=entry_points,
                            node_min=node_min, node_max=node_max,
                            config_fingerprint=current_fingerprint,
                            generate_descriptions=generate_descriptions,
                            existing_for_descriptions=existing,
                            regenerate_descriptions=regenerate_descriptions)
    else:
        graph = build_graph(root, provider_name=provider_name, no_llm=no_llm,
                            repo_hash=current_hash, extra_ignore=extra_ignore,
                            model=model, cache_base=cache_base,
                            entry_points=entry_points,
                            node_min=node_min, node_max=node_max,
                            config_fingerprint=current_fingerprint,
                            generate_descriptions=generate_descriptions,
                            existing_for_descriptions=existing,
                            regenerate_descriptions=regenerate_descriptions)

    write_graph(root, graph)
    return graph, False
