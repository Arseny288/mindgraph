"""describe.py — build-time node/domain descriptions (item 7).

Descriptions are generated ONCE, during graph construction (see
:func:`build_descriptions`, called from emit.build_graph), and stored on the
node/domain dict itself. The viewer never calls a model — it only reads
``node["description"]`` off the loaded graph.json.

Each node and domain gets:
    description               str | None  — 2-4 plain-language sentences
    description_hash          str         — sha256 of the node's boundary
                                              content (see node_content_hash)
    description_model         str         — the model that produced it
    description_generated_at  str         — ISO timestamp

Invalidation is content-based, not mtime-based: a node is regenerated only
when its ``description_hash`` changes from the previous graph.json (or
unconditionally under ``force``/``--regenerate-descriptions``). Generation is
concurrent (a bounded worker pool), each node gets a couple of retries and an
overall per-node timeout; a node that still fails after that gets
``description: None`` and the run completes anyway — a failed description is
never a build failure.
"""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("mindgraph.describe")

MAX_CONCURRENT = 5
PER_NODE_TIMEOUT_S = 45
MAX_RETRIES = 2

_SYSTEM_PROMPT = ("Answer in plain, natural-language prose only: 2 to 4 sentences, "
                  "no markdown, no headers, no bullet lists, no restating of file names.")

_NODE_PROMPT = """You are documenting one semantic area of a codebase, for a
developer who already knows how to code but has never seen this repo. Cover,
in plain language: (1) what this area is responsible for, (2) what it
depends on, (3) what depends on it.

Area: {name}
Symbols (id: signature — docstring):
{symbols}

Depends on (other areas): {outbound}
Used by (other areas): {inbound}
"""

_DOMAIN_PROMPT = """You are documenting one top-level domain of a codebase (a
group of semantic areas), for a developer who already knows how to code but
has never seen this repo. Cover, in plain language: (1) what this domain is
responsible for, (2) what it depends on, (3) what depends on it.

Domain: {name}
Areas inside it: {members}
Depends on (other domains): {outbound}
Used by (other domains): {inbound}
"""


def node_content_hash(root: str, files: list) -> str:
    """Hash of the ACTUAL CONTENT of a node's files, not the file list and
    not mtimes — a node's boundary is what its files contain (files carry
    the symbols), so reading them covers "files and symbols" together. Order
    is normalized (sorted) so the hash is stable between runs."""
    import os
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.encode("utf-8"))
        h.update(b"\x00")
        try:
            with open(os.path.join(root, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
        h.update(b"\x00")
    return h.hexdigest()


def _node_prompt(node: dict, symbols_by_id: dict, node_edges: list, nodes_by_id: dict) -> str:
    syms = [symbols_by_id[sid] for sid in node.get("symbols", []) if sid in symbols_by_id]
    syms.sort(key=lambda s: s["id"])
    lines = []
    for s in syms:
        if s.get("synthetic"):
            continue
        doc = (s.get("doc") or "").strip().replace("\n", " ")[:160]
        lines.append(f"- {s['id']}: {s.get('sig', '')} — {doc}")
    nid = node["id"]
    outbound = sorted({nodes_by_id[e["to"]]["name"] for e in node_edges
                       if e["from"] == nid and e["to"] in nodes_by_id})
    inbound = sorted({nodes_by_id[e["from"]]["name"] for e in node_edges
                      if e["to"] == nid and e["from"] in nodes_by_id})
    return _NODE_PROMPT.format(
        name=node["name"], symbols="\n".join(lines) or "(none)",
        outbound=", ".join(outbound) or "nothing",
        inbound=", ".join(inbound) or "nothing",
    )


def _domain_prompt(domain: dict, nodes: list, domain_edges: list, domains_by_id: dict) -> str:
    members = sorted(n["name"] for n in nodes if n.get("parent") == domain["id"])
    did = domain["id"]
    outbound = sorted({domains_by_id[e["to"]]["name"] for e in domain_edges
                       if e["from"] == did and e["to"] in domains_by_id})
    inbound = sorted({domains_by_id[e["from"]]["name"] for e in domain_edges
                      if e["to"] == did and e["from"] in domains_by_id})
    return _DOMAIN_PROMPT.format(
        name=domain["name"], members=", ".join(members) or "(none)",
        outbound=", ".join(outbound) or "nothing",
        inbound=", ".join(inbound) or "nothing",
    )


def _generate_with_retries(provider, prompt: str) -> tuple:
    """Up to 1 try + MAX_RETRIES retries. Raises the last error if all fail."""
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = provider.complete(_SYSTEM_PROMPT, prompt, max_tokens=400)
            text = (resp.get("text") or "").strip()
            if text:
                return text, (resp.get("usage") or {})
            last_exc = RuntimeError("empty response")
        except Exception as e:  # noqa: BLE001 — any provider failure is a retry candidate
            last_exc = e
        if attempt < MAX_RETRIES:
            time.sleep(0.5 * (attempt + 1))
    raise last_exc or RuntimeError("generation failed")


def build_descriptions(root: str, graph: dict, provider, existing_graph: dict | None = None,
                       force: bool = False, model_name: str = "") -> dict:
    """Mutates ``graph["nodes"]`` and ``graph["domains"]`` in place, adding the
    four description_* fields to each. Returns a summary:
    ``{"total", "described", "failed": [(name, reason), ...], "reused", "usage"}``.
    """
    nodes = graph.get("nodes", [])
    domains = graph.get("domains", [])
    node_edges = graph.get("node_edges", [])
    domain_edges = graph.get("domain_edges", [])
    symbols_by_id = {s["id"]: s for s in graph.get("symbols", [])}
    nodes_by_id = {n["id"]: n for n in nodes}
    domains_by_id = {d["id"]: d for d in domains}

    existing_nodes = {n["id"]: n for n in (existing_graph or {}).get("nodes", [])}
    existing_domains = {d["id"]: d for d in (existing_graph or {}).get("domains", [])}

    def reuse(obj: dict, prev: dict | None, h: str) -> bool:
        if force or not prev:
            return False
        if prev.get("description_hash") != h or prev.get("description") is None:
            return False
        obj["description"] = prev["description"]
        obj["description_hash"] = h
        obj["description_model"] = prev.get("description_model", model_name)
        obj["description_generated_at"] = prev.get("description_generated_at", "")
        return True

    tasks = []  # (obj, hash, prompt)
    for n in nodes:
        h = node_content_hash(root, n.get("files", []))
        if not reuse(n, existing_nodes.get(n["id"]), h):
            tasks.append((n, h, _node_prompt(n, symbols_by_id, node_edges, nodes_by_id)))
    for d in domains:
        member_files = sorted({f for n in nodes if n.get("parent") == d["id"]
                               for f in n.get("files", [])})
        h = node_content_hash(root, member_files)
        if not reuse(d, existing_domains.get(d["id"]), h):
            tasks.append((d, h, _domain_prompt(d, nodes, domain_edges, domains_by_id)))

    total = len(nodes) + len(domains)
    reused = total - len(tasks)
    described, failed = [], []
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    now = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731

    if tasks:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
            future_to_task = {ex.submit(_generate_with_retries, provider, prompt): (obj, h)
                              for obj, h, prompt in tasks}
            for fut in as_completed(future_to_task):
                obj, h = future_to_task[fut]
                try:
                    text, usage = fut.result(timeout=PER_NODE_TIMEOUT_S)
                    obj["description"] = text
                    obj["description_hash"] = h
                    obj["description_model"] = model_name
                    obj["description_generated_at"] = now()
                    described.append(obj["name"])
                    usage_totals["input_tokens"] += usage.get("input_tokens", 0)
                    usage_totals["output_tokens"] += usage.get("output_tokens", 0)
                except Exception as e:  # noqa: BLE001 — timeout or exhausted retries
                    obj["description"] = None
                    obj["description_hash"] = h
                    obj["description_model"] = model_name
                    obj["description_generated_at"] = now()
                    failed.append((obj["name"], str(e)))
                    log.warning("description failed for %r: %s", obj["name"], e)

    return {"total": total, "described": len(described), "failed": failed,
           "reused": reused, "usage": usage_totals}
