"""model.py — the ownership and contracts model on top of graph.json (phase 1).

Answers "what a node may edit", "what it reads by contract", "what is out of
reach for it". Everything is mechanical, without an LLM:

* symbol -> node: the ``symbols[].node`` field (already computed in graph.json);
* file -> owning node: the majority of the file's REAL symbols (tie-break —
  lexicographically by node name); a file without real symbols inherits the node
  of its pseudo-``<module>``;
* contract: a symbol with ``shared == true`` (used from more than one node); its
  signature is pinned by the hash ``sha256(sig)`` and cannot change without
  explicit agreement.

All of this is read-only analysis of the already-built ``graph.json``; nothing
is recomputed from sources and no LLM is called.
"""

from __future__ import annotations

import hashlib
import json
import os

from . import store


def load_graph(root: str) -> dict:
    """Read the project's built graph.json (see :mod:`mindgraph.store` —
    the storage amendment: it lives under the tool's own storage root, never
    inside the analyzed repo)."""
    path = store.resolve_project(root, touch=False).graph_path
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Three verdict states (phase 1.5): phase 2 and the viewer rely on them.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNVERIFIED = "unverified"


def unresolved_imports(graph: dict) -> list:
    """Unresolved imports — external packages the extractor could not reduce to
    in-repo symbols (graph incompleteness)."""
    return list(graph.get("stats", {}).get("external", []))


def graph_incomplete(graph: dict) -> bool:
    """The graph is incomplete: there are unresolved imports -> impact/verify understate."""
    return bool(unresolved_imports(graph))


def signature_hash(sig: str | None) -> str:
    """Pinned hash of a contract's signature."""
    return "sha256:" + hashlib.sha256((sig or "").encode("utf-8")).hexdigest()[:16]


def node_by_ref(graph: dict, ref: str) -> dict:
    """Find an L1 node by id or by name (accepts both)."""
    for n in graph["nodes"]:
        if n["id"] == ref or n["name"] == ref:
            return n
    raise KeyError(f"node {ref!r} not found in graph.json")


def _symbol_by_id(graph: dict) -> dict:
    return {s["id"]: s for s in graph["symbols"]}


def file_owner_map(graph: dict) -> dict:
    """``{file: node_name}`` — the owning node of each file (majority of symbols)."""
    counts: dict = {}
    for s in graph["symbols"]:
        if s.get("synthetic"):
            continue
        counts.setdefault(s["file"], {})
        counts[s["file"]][s["node"]] = counts[s["file"]].get(s["node"], 0) + 1

    owners: dict = {}
    for f, c in counts.items():
        # max by symbol count; on a tie — the lexicographically first node.
        owners[f] = max(sorted(c), key=lambda n: c[n])

    # Files without real symbols: inherit the node of their <module>.
    for s in graph["symbols"]:
        if s.get("synthetic") and s["file"] not in owners:
            owners[s["file"]] = s["node"]
    return owners


def file_symbol_nodes(graph: dict) -> dict:
    """``{file: {node_name: [symbol_ids]}}`` for REAL symbols."""
    by_file: dict = {}
    for s in graph["symbols"]:
        if s.get("synthetic"):
            continue
        by_file.setdefault(s["file"], {}).setdefault(s["node"], []).append(s["id"])
    return by_file


def mixed_files(graph: dict) -> dict:
    """``{file: {owner, nodes, foreign}}`` — files with symbols of >1 node.

    ``foreign`` — symbols (by node) belonging NOT to the file's owner. This is a
    hole in the boundary: the owner of a mixed file can edit someone else's
    symbol, and a path-based check does not see it — so verify breaks the diff
    down to symbols.
    """
    owners = file_owner_map(graph)
    fn = file_symbol_nodes(graph)
    mixed: dict = {}
    for f, nodes in fn.items():
        if len(nodes) > 1:
            owner = owners.get(f, "?")
            mixed[f] = {
                "owner": owner,
                "nodes": {n: sorted(ids) for n, ids in sorted(nodes.items())},
                "foreign": {n: sorted(ids) for n, ids in sorted(nodes.items())
                            if n != owner},
            }
    return mixed


def foreign_symbol_ranges(graph: dict, file: str) -> list:
    """Foreign symbols of a mixed file with their line ranges.

    Needed by the agent (phase 2.1): ``edit_file`` checks the touched ranges
    BEFORE writing and refuses if they belong to a foreign symbol.
    """
    m = mixed_files(graph).get(file)
    if m is None:
        return []
    sym_by_id = _symbol_by_id(graph)
    out = []
    for node, ids in sorted(m["foreign"].items()):
        for sid in ids:
            s = sym_by_id.get(sid)
            if s is not None:
                out.append({"symbol": sid, "node": node, "lines": s["lines"]})
    return out


def mixed_file_stats(graph: dict) -> dict:
    """File-mixedness metric for the report (phase 1.5)."""
    fn = file_symbol_nodes(graph)
    mixed = mixed_files(graph)
    total_files = len(fn)
    total_real = sum(len(ids) for nodes in fn.values() for ids in nodes.values())
    foreign_count = sum(len(ids) for m in mixed.values()
                        for ids in m["foreign"].values())
    return {
        "total_files": total_files,
        "mixed_files": len(mixed),
        "mixed_file_ratio": (len(mixed) / total_files) if total_files else 0.0,
        "foreign_symbols_in_mixed": foreign_count,
        "total_real_symbols": total_real,
        "foreign_ratio": (foreign_count / total_real) if total_real else 0.0,
    }


def contract_symbols(graph: dict) -> list:
    """All contract (shared) symbols with a pinned signature."""
    return [{"symbol": s["id"], "node": s["node"],
             "signature_hash": signature_hash(s["sig"])}
            for s in graph["symbols"]
            if s.get("shared") and not s.get("synthetic")]


def node_contracts(graph: dict, node: dict) -> list:
    """Contracts that the node USES (depends on) but does not own.

    The ``from`` edge belongs to a symbol of the node, ``to`` — to a shared
    symbol of another node. Reading is allowed, changing the signature is not.
    """
    sym_by_id = _symbol_by_id(graph)
    owned = {s["id"] for s in graph["symbols"]
             if s["node"] == node["name"] and not s.get("synthetic")}
    used: dict = {}
    for e in graph["symbol_edges"]:
        if e["from"] not in owned:
            continue
        target = sym_by_id.get(e["to"])
        if target is None or not target.get("shared") \
                or target["node"] == node["name"]:
            continue
        used[e["to"]] = target
    return [{"symbol": sid, "node": sym_by_id[sid]["node"],
             "signature_hash": signature_hash(sym_by_id[sid]["sig"])}
            for sid in sorted(used)]


def node_view(graph: dict, node: dict) -> dict:
    """Everything the ``node`` command needs: owned / contracts / foreign / mixed."""
    owners = file_owner_map(graph)
    owned_files = sorted(f for f, owner in owners.items()
                         if owner == node["name"])
    owned_symbols = sorted(
        (s for s in graph["symbols"]
         if s["node"] == node["name"] and not s.get("synthetic")),
        key=lambda s: s["id"])
    contracts = node_contracts(graph, node)

    all_files = {s["file"] for s in graph["symbols"]}
    foreign_files = sorted(all_files - set(owned_files))

    mixed = mixed_files(graph)
    owned_mixed = [
        {"file": f, "owner": mixed[f]["owner"],
         "foreign_symbols": sorted(
             sid for ids in mixed[f]["foreign"].values() for sid in ids),
         "foreign_nodes": sorted(mixed[f]["foreign"]),
         "foreign_ranges": foreign_symbol_ranges(graph, f)}
        for f in owned_files if f in mixed and mixed[f]["owner"] == node["name"]
    ]

    return {
        "owned_files": owned_files,
        "owned_symbols": [s["id"] for s in owned_symbols],
        "contracts": contracts,
        "foreign_files": foreign_files,
        "mixed": owned_mixed,
    }


def allow_write_globs(graph: dict, node: dict) -> list:
    """Write permissions: a directory glob if the directory WHOLLY belongs to the
    node, otherwise exact file paths (the directory is shared, ``dir/**`` would
    intercept foreign files). Plus mirrored test directories."""
    owners = file_owner_map(graph)
    all_files = {s["file"] for s in graph["symbols"]}
    owned = sorted(f for f, owner in owners.items() if owner == node["name"])

    dir_of: dict = {}
    for f in all_files:
        dir_of.setdefault(os.path.dirname(f), set()).add(f)

    by_dir: dict = {}
    for f in owned:
        by_dir.setdefault(os.path.dirname(f), []).append(f)

    entries = set()
    for d, fs in sorted(by_dir.items()):
        if set(fs) == dir_of.get(d, set()):
            entries.add((d + "/**") if d else "**")
        else:
            entries.update(fs)  # the directory is split among several nodes — pointwise

    # Mirrored tests: directory `X/sub` corresponds to `tests/sub/**`.
    extra = set()
    for f in owned:
        parts = f.split("/")
        for i in range(1, len(parts)):
            cand = "tests/" + "/".join(parts[i:])
            if any(f2 == cand or f2.startswith(cand + "/")
                   for f2 in all_files):
                extra.add(cand + "/**")
                break
    return sorted(entries | extra)


def allow_create_globs(graph: dict, node: dict) -> list:
    """Globs of directories WHOLLY owned by the node — new files can be created
    there (allow_write gives exact paths and does not cover creation)."""
    owners = file_owner_map(graph)
    all_files = {s["file"] for s in graph["symbols"]}
    owned = {f for f, o in owners.items() if o == node["name"]}

    dir_of: dict = {}
    for f in all_files:
        dir_of.setdefault(os.path.dirname(f), set()).add(f)

    fully_owned = {d for d, files in dir_of.items() if files and files <= owned}

    def _has_owned_ancestor(d: str) -> bool:
        parts = d.split("/")
        return any("/".join(parts[:i]) in fully_owned
                   for i in range(1, len(parts)))

    return sorted({(d + "/**") if d else "**"
                   for d in fully_owned if not _has_owned_ancestor(d)})


def boundary(graph: dict, node: dict) -> dict:
    """The node's zone boundaries: allow_write / allow_create / deny_write / allow_read.

    Resolution order — deny by default (``**``), allow pointwise.
    ``allow_read`` = the write zone + EXACT paths of the files of contract symbols.
    ``mixed`` — the mixed files of the zone (foreign symbols cannot be edited,
    even if the path is in allow_write).
    """
    contracts = node_contracts(graph, node)
    sym_by_id = _symbol_by_id(graph)

    allow_write = allow_write_globs(graph, node)
    allow_create = allow_create_globs(graph, node)
    contract_files = sorted({sym_by_id[c["symbol"]]["file"]
                             for c in contracts
                             if c["symbol"] in sym_by_id})
    allow_read = sorted(set(allow_write) | set(contract_files))

    mixed = mixed_files(graph)
    owners = file_owner_map(graph)
    owned_files = sorted(f for f, o in owners.items() if o == node["name"])
    mixed_here = [
        {"file": f, "owner": mixed[f]["owner"],
         "foreign_symbols": sorted(
             sid for ids in mixed[f]["foreign"].values() for sid in ids),
         "foreign_nodes": sorted(mixed[f]["foreign"]),
         "foreign_ranges": foreign_symbol_ranges(graph, f)}
        for f in owned_files if f in mixed
    ]

    return {
        "schema_version": "1",
        "node_id": node["id"],
        "node_name": node["name"],
        "allow_write": allow_write,
        "allow_create": allow_create,
        "deny_write": ["**"],
        "allow_read": allow_read,
        "contracts": contracts,
        "mixed": mixed_here,
    }
