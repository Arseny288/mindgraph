"""Stage C — computing the shared layer.

The metric answers the question: **if node A edits symbol S, who breaks.** It
breaks for whoever depends on S directly — therefore ``owners`` is computed over
DIRECT reverse edges, without a depth-first traversal:

    owners(S) = { node(P) | edge P -> S of type call | uses | inherits }

Why not the transitive closure: reachability without a depth limit measures the
graph's connectedness ("can you crawl from a node to a symbol along a chain"),
and in any connected program the answer is almost always "yes". That inflates
shared into a measure of connectivity rather than of isolability.

What is excluded from ownership:

* ``contains`` (class -> method) — a method belonging to a class is not a
  dependency. If the method is actually called from outside, there is already a
  direct ``call``/``uses`` edge for it. The ``contains`` edge itself stays in the
  graph — grouping at stage 3 needs it.
* entry points (``ENTRY_POINT_PATTERNS``) — shell-context / app-runner by
  definition touch everything, so they are not counted as owners (on par with
  ``tests.py`` and ``migrations/``, already excluded from the walk).

A transitive version is kept nearby as :func:`compute_reach` — it answers a
different useful question ("what a node is capable of touching at all");
``reach_ratio`` is taken from it for comparison.

In no-LLM mode the role of the "node" is played by the package (the directory of
the symbol's file), ``proxy=package``. At stage 3 this is replaced by the nodes
of LLM grouping.

Known limitation of ``proxy=package``: for a repo with a single flat package (all
files in one directory, without subpackages — e.g. ``psf/requests``, where
``models.py``/``sessions.py``/``utils.py`` sit side by side) the whole library
collapses into one node, and cross-file usage becomes intra-node — ``shared_ratio``
is understated almost to zero, although there is plenty of real sharing (via
``models``/``sessions``/``utils``). This is not a loss of edges, but a coarsening
of the comparison unit: under ``proxy=module`` the same repo gives a sensible
picture (see the run on requests: 0.2% -> 5.4%, top shared — exactly
``models.PreparedRequest``/``models.Response``/``structures.CaseInsensitiveDict``).
When comparing ``shared_ratio`` between different repos by ``package``, account
for this asymmetry separately; the stage 3 LLM-grouping node will close it for
good.
"""

from __future__ import annotations

import fnmatch

# Edges over which ownership is counted (contains is intentionally excluded).
OWNER_EDGE_TYPES = frozenset({"call", "uses", "inherits"})

# Entry points: their symbols are not counted as owners. A list of patterns
# (glob by path or by file name), not a single hardcoded file.
ENTRY_POINT_PATTERNS = ("wsgi.py", "asgi.py", "manage.py", "run.py", "app.py",
                        "__main__.py")


def node_of(file: str, proxy: str = "package") -> str:
    """A symbol's node under the chosen structural proxy.

    ``package`` (default) — the file's directory (`app/api/x.py` -> `app.api`);
    ``module``  — the file itself as a module (`app/api/x.py` -> `app.api.x`).
    """
    if proxy == "module":
        mod = file[:-3] if file.endswith(".py") else file
        parts = mod.split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else "<root>"
    directory = file.rsplit("/", 1)[0] if "/" in file else ""
    return directory.replace("/", ".") if directory else "<root>"


def _is_entry_point(file: str, patterns) -> bool:
    base = file.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(file, p) or fnmatch.fnmatch(base, p)
               for p in patterns)


def compute_owners(symbols, edges, entry_points=ENTRY_POINT_PATTERNS,
                   proxy="package") -> dict:
    """Direct owners: {symbol_id: {'node', 'owners': [...], 'shared': bool}}.

    owners(S) — nodes of S's direct predecessors along call/uses/inherits edges,
    excluding predecessors from entry points. The node is a structural proxy
    (package/module); see :func:`compute_owners_by_node` for an arbitrary
    symbol -> node mapping (stage 3, LLM grouping).
    """
    node = {s["id"]: node_of(s["file"], proxy) for s in symbols}
    entry = {s["id"] for s in symbols
             if _is_entry_point(s["file"], entry_points)}
    return compute_owners_by_node(symbols, edges, node, entry_ids=entry)


def compute_owners_by_node(symbols, edges, node_of_symbol, entry_ids=frozenset()) -> dict:
    """Like :func:`compute_owners`, but the node is given by an explicit mapping
    ``{symbol_id: node_name}`` instead of a structural proxy, and entry points —
    by a ready set of ids (rather than file glob patterns).

    Used for semantic nodes from LLM grouping (stage 3) — there the node is not
    derived from the file path but comes from the model.
    """
    owners: dict = {sid: set() for sid in node_of_symbol}
    for e in edges:
        if e["type"] not in OWNER_EDGE_TYPES:
            continue
        src, dst = e["from"], e["to"]
        if dst not in owners or src not in node_of_symbol or src in entry_ids:
            continue
        owners[dst].add(node_of_symbol[src])

    return {
        sid: {"node": node_of_symbol[sid], "owners": sorted(ns), "shared": len(ns) > 1}
        for sid, ns in owners.items()
    }


def compute_reach(symbols, edges, proxy="package") -> dict:
    """Transitive version (old): reverse traversal over ALL edges, with visited.

    Answers the question "what a node is capable of touching at all". The
    symbol's own node always enters (a symbol is reachable from itself).
    """
    node = {s["id"]: node_of(s["file"], proxy) for s in symbols}
    ancestors: dict = {}
    for e in edges:
        ancestors.setdefault(e["to"], set()).add(e["from"])

    result = {}
    for sid in node:
        seen = set()
        stack = [sid]
        while stack:  # visited quenches cycles
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(ancestors.get(cur, ()))
        owners = sorted({node[x] for x in seen if x in node})
        result[sid] = {"owners": owners, "shared": len(owners) > 1}
    return result


def summarize(symbols, edges, entry_points=ENTRY_POINT_PATTERNS,
              proxy="package") -> dict:
    """Summary: shared_ratio (direct) and reach_ratio (transitive) side by side.

    The metric is computed over REAL symbols (function/method/class/binding).
    Synthetic pseudo-<module> symbols do not enter the denominator — they are not
    units of code, but top-level roots/holders; they participate in edges as
    sources of ownership.
    """
    direct = compute_owners(symbols, edges, entry_points, proxy)
    reach = compute_reach(symbols, edges, proxy)

    measured = [s["id"] for s in symbols if not s.get("synthetic")]
    total = len(measured)

    shared = {sid: direct[sid] for sid in measured if direct[sid]["shared"]}
    reach_shared = sum(1 for sid in measured if reach[sid]["shared"])

    hist: dict = {}
    for sid in measured:
        n = len(direct[sid]["owners"])
        hist[n] = hist.get(n, 0) + 1

    ranked = sorted(shared.items(),
                    key=lambda kv: (-len(kv[1]["owners"]), kv[0]))

    return {
        "symbols": total,
        "synthetic": len(symbols) - total,
        "proxy": proxy,
        "nodes": sorted({r["node"] for r in direct.values()}),
        "shared_count": len(shared),
        "shared_ratio": (len(shared) / total) if total else 0.0,
        "reach_shared_count": reach_shared,
        "reach_ratio": (reach_shared / total) if total else 0.0,
        "owners_hist": dict(sorted(hist.items())),
        "top_shared": [(sid, r["owners"]) for sid, r in ranked],
        "owners": direct,
    }


def summarize_by_node(symbols, edges, node_of_symbol, entry_ids=frozenset()) -> dict:
    """Like :func:`summarize`, but on an explicit symbol -> node mapping (stage 3).

    Without ``reach_ratio`` — the transitive overview only makes sense for the
    structural proxy (§ module docstring); on semantic nodes the direct
    ``shared_ratio`` is enough for comparison with package/module.
    """
    direct = compute_owners_by_node(symbols, edges, node_of_symbol, entry_ids)

    measured = [s["id"] for s in symbols if not s.get("synthetic")]
    total = len(measured)

    shared = {sid: direct[sid] for sid in measured if direct[sid]["shared"]}

    hist: dict = {}
    for sid in measured:
        n = len(direct[sid]["owners"])
        hist[n] = hist.get(n, 0) + 1

    ranked = sorted(shared.items(),
                    key=lambda kv: (-len(kv[1]["owners"]), kv[0]))

    return {
        "symbols": total,
        "synthetic": len(symbols) - total,
        "nodes": sorted({r["node"] for r in direct.values()}),
        "shared_count": len(shared),
        "shared_ratio": (len(shared) / total) if total else 0.0,
        "owners_hist": dict(sorted(hist.items())),
        "top_shared": [(sid, r["owners"]) for sid, r in ranked],
        "owners": direct,
    }
