"""Stage B2 — LLM grouping of L1 nodes into L0 domains.

A SEPARATE second model request, fully independent of group.py: group.py and
the symbol-to-L1-node assignment (§3 of the spec) stay untouched — this module
does not import them for mutation and does not touch their file.

The second request receives ONLY the already-computed L1 nodes (name, summary,
sizes) and the edges between them — symbol lists are NOT passed at all (the
model must neither see nor change what was already decided at stage B). The
model's response sets a ``parent`` field (domain id) on each L1 node and
returns the list of domains. :func:`assign_domains` is the single mutation
point: it never touches ``node["symbols"]``, and the caller (emit.py) verifies
this with an assert before/after the call (see build_graph).

If the LLM is unavailable (no key/package) or the response does not parse —
silent fallback to a single "Project" domain containing all nodes (the same
"never fail wholesale" principle as in group.py/emit.py), with a warning.
"""

from __future__ import annotations

import os
import re

# Read-only import of ready constants/utils from group.py — group.py itself is
# not modified or reopened by this module.
from . import llmcache
from .group import (
    ANTHROPIC_MODEL, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, _parse_json_response,
)

UNASSIGNED_DOMAIN = "Project"

_DOMAIN_SYSTEM_PROMPT = """Group the nodes of the code graph into 4-6 top-level domains — by the semantic
closeness of their purpose, not alphabetically and not by size.

Domain names — short, in English, in domain terms (e.g. "Data handling",
"User interaction").

Every node in the list must be assigned to exactly one domain.

Answer STRICTLY in JSON, without markdown fences (```), without preamble and
without explanations before or after. Response format:

{"domains": [{"name": "Data handling", "nodes": ["Storage", "Models"]}]}
"""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", name.strip().lower()).strip("_")
    return s or "domain"


def _build_domain_user_prompt(nodes: list, node_edges: list) -> str:
    """Only the L1 nodes' names/summary/sizes and the edges between them — not
    a single symbol (see the module docstring)."""
    import json
    slim_nodes = [
        {"name": n["name"], "summary": n.get("summary", ""),
         "files": len(n["files"]), "symbols": len(n["symbols"]), "loc": n["loc"]}
        for n in nodes
    ]
    name_by_id = {n["id"]: n["name"] for n in nodes}
    slim_edges = [
        {"from": name_by_id[e["from"]], "to": name_by_id[e["to"]], "weight": e["weight"]}
        for e in node_edges if e["from"] in name_by_id and e["to"] in name_by_id
    ]
    payload = {"nodes": slim_nodes, "edges": slim_edges}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}


def _call_deepseek(system_prompt: str, user_prompt: str,
                   model: str | None = None) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set (neither as an argument nor in env/.env)")
    from openai import OpenAI  # lazy import, as in group.py
    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=model or DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2000,  # the L1 node list (5-15 items) is an order of magnitude smaller than the symbol list
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    text = resp.choices[0].message.content or ""
    u = resp.usage
    usage = _empty_usage()
    if u is not None:
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        usage.update({
            "input_tokens": (u.prompt_tokens or 0) - hit,
            "output_tokens": u.completion_tokens or 0,
            "cache_read_tokens": hit,
            "cache_write_tokens": 0,
            "raw": u.model_dump() if hasattr(u, "model_dump") else dict(u),
        })
    return {"text": text, "usage": usage}


def _call_anthropic(system_prompt: str, user_prompt: str,
                    model: str | None = None) -> dict:
    import anthropic  # lazy import
    key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    resp = client.messages.create(
        model=model or ANTHROPIC_MODEL,
        max_tokens=2000,
        system=system_prompt,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    usage = _empty_usage()
    if u is not None:
        usage.update({
            "input_tokens": u.input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "raw": u.model_dump() if hasattr(u, "model_dump") else dict(u),
        })
    return {"text": text, "usage": usage}


def _call_provider(provider_name: str, system_prompt: str, user_prompt: str,
                   model: str | None = None) -> dict:
    if provider_name == "deepseek":
        return _call_deepseek(system_prompt, user_prompt, model)
    if provider_name == "anthropic":
        return _call_anthropic(system_prompt, user_prompt, model)
    raise ValueError(f"unknown provider {provider_name!r}")


def _validate_domains(parsed, nodes: list, warnings: list) -> list | None:
    """[{"name", "members": [node_name, ...]}] or None (fallback to one domain)."""
    valid_names = {n["name"] for n in nodes}
    raw_domains = parsed.get("domains") if isinstance(parsed, dict) else None
    if not isinstance(raw_domains, list) or not raw_domains:
        warnings.append("model response has no domains list, falling back to one domain")
        return None

    result = []
    assigned = set()
    for rd in raw_domains:
        if not isinstance(rd, dict) or "name" not in rd:
            warnings.append(f"skipped malformed domain in model response: {rd!r}")
            continue
        members = []
        for name in rd.get("nodes", []):
            if name not in valid_names:
                warnings.append(f"domain {rd['name']!r}: node {name!r} does not exist, dropped")
                continue
            if name in assigned:
                warnings.append(f"node {name!r} assigned to more than one domain, kept in the first")
                continue
            members.append(name)
            assigned.add(name)
        if members:
            result.append({"name": rd["name"], "members": members})

    unassigned = sorted(valid_names - assigned)
    if unassigned:
        result.append({"name": UNASSIGNED_DOMAIN, "members": unassigned})

    if not result:
        warnings.append("no domains left after validation, falling back to one domain")
        return None
    return result


def _finalize(buckets: list, nodes: list) -> list:
    """Set node["parent"] and return the final domains array for graph.json.

    The only field mutated on the nodes is ``parent``.
    ``node["symbols"]`` is never read or written here.
    """
    node_by_name = {n["name"]: n for n in nodes}
    out = []
    for b in buckets:
        did = "d_" + _slug(b["name"])
        out.append({"id": did, "name": b["name"]})
        for member_name in b["members"]:
            node_by_name[member_name]["parent"] = did
    return out


def assign_domains(nodes: list, node_edges: list, provider_name: str = "deepseek",
                   no_llm: bool = False, cache_base=None,
                   model: str | None = None) -> dict:
    """Stage B2 orchestration. Mutates ``nodes`` (adds ``parent``).

    ``cache_base`` — the LLM-response cache directory; ``model`` — an override
    of the provider's default model (otherwise DEEPSEEK_MODEL/ANTHROPIC_MODEL).

    Returns ``{"domains": [...], "warnings": [...], "usage": {...},
    "raw_text": str}``. When the LLM/provider is missing or the response is
    invalid — a single :data:`UNASSIGNED_DOMAIN` for all nodes, without raising
    (§1 of the spec: never fail wholesale).
    """
    warnings: list = []
    usage = _empty_usage()
    raw_text = ""
    buckets = None

    if not no_llm:
        system = _DOMAIN_SYSTEM_PROMPT
        user = _build_domain_user_prompt(nodes, node_edges)
        eff_model = model or (DEEPSEEK_MODEL if provider_name == "deepseek"
                              else ANTHROPIC_MODEL)
        cache_key = llmcache.prompt_key(provider_name, eff_model, system, user)
        resp = None
        if cache_base is not None:
            resp = llmcache.load(cache_base, cache_key)
        if resp is None:
            try:
                resp = _call_provider(provider_name, system, user, model)
            except Exception as e:  # no key, no package, network error, etc.
                warnings.append(
                    f"LLM provider {provider_name!r} unavailable for domains ({e}); "
                    f"single domain {UNASSIGNED_DOMAIN!r} for all nodes")
                resp = None
            if resp is not None and cache_base is not None:
                llmcache.save(cache_base, cache_key, resp["text"], resp["usage"],
                              provider_name, eff_model)
        if resp is not None:
            raw_text = resp["text"]
            usage = resp["usage"]
            parsed, err = _parse_json_response(raw_text)
            if err:
                warnings.append(err)
            else:
                buckets = _validate_domains(parsed, nodes, warnings)

    if buckets is None:
        buckets = [{"name": UNASSIGNED_DOMAIN, "members": [n["name"] for n in nodes]}]

    domains = _finalize(buckets, nodes)
    return {"domains": domains, "warnings": warnings, "usage": usage, "raw_text": raw_text}
