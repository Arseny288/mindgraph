"""Stage B — LLM grouping of symbols into semantic nodes.

The provider is abstracted behind :class:`LLMProvider` with a single
``group(symbols, edges) -> dict`` method. Both implementations
(:class:`DeepSeekProvider`, :class:`AnthropicProvider`) use the SAME prompt —
`SYSTEM_PROMPT` and :func:`_build_user_prompt` — so provider comparison is
honest: the only difference is the API client, not the request text. Response
parsing/validation (§3 of the spec) is also shared, outside the provider — see
:func:`group_repo`.

Prompt ordering: the immutable part (system instruction, symbol and edge list)
comes first; there is no volatile part yet — the whole graph is static per run.
This gives DeepSeek a stable cacheable prefix out of the box.

Batching (§3 of the spec): when there are more than ``BATCH_MAX_SYMBOLS`` real
symbols, split them into batches BY MODULE (deterministically), group each batch
with the normal prompt, then MERGE close nodes by name and summary with a SECOND
request (``_merge_nodes``). A partial batch failure (invalid response) does not
crash the run: that batch's symbols go to ``UNASSIGNED_NODE`` with a warning.

**Function bodies never reach the prompt** — only `id`, `sig`, `doc` and edges
between ids.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod

from . import llmcache

log = logging.getLogger("mindgraph.group")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
# deepseek-chat / deepseek-reasoner have been disabled since 2026-07-24 — do not use.

ANTHROPIC_MODEL = "claude-opus-5"  # see the note in the AnthropicProvider docstring

UNASSIGNED_NODE = "Unassigned"

# Threshold: when there are more real symbols than this, group in batches (§3).
BATCH_MAX_SYMBOLS = 1500

_SYSTEM_PROMPT_TEMPLATE = """Group the symbols of a Python repository into {count_spec} semantic nodes by PURPOSE,
not by file location. A single node usually spans multiple files; a single file
may belong to multiple nodes.

Node names — short, in English, in domain terms (e.g. "Authentication",
"Storage", "Notifications"), NOT in implementation terms (not "auth module",
not "app.api").

Every symbol in the list must be assigned to exactly one node.

Answer STRICTLY in JSON, without markdown fences (```), without preamble and
without explanations before or after. Response format:

{{"nodes": [{{"name": "Authentication",
            "summary": "one sentence about the node's purpose",
            "symbols": ["auth.login.verify_token", "auth.tokens.issue"]}}]}}
"""


def _system_prompt(node_count: int | None = None, node_min: int = 5,
                   node_max: int = 15) -> str:
    """Prompt with a node range (``node_min``-``node_max``) or an exact K.

    ``node_count`` is used only for control runs comparing against a fixed
    structural-proxy granularity (see ``run_confound.py`` — K=6 vs a package
    proxy on 6 nodes). ``node_min``/``node_max`` come from config
    (`[llm] node_min/node_max`) and control grouping "freedom".
    """
    if node_count is not None:
        count_spec = f"exactly {node_count}"
    elif node_min == node_max:
        count_spec = f"exactly {node_min}"
    else:
        count_spec = f"{node_min}-{node_max}"
    return _SYSTEM_PROMPT_TEMPLATE.format(count_spec=count_spec)


# Manifest-mode prompt: existing nodes are referenced by STABLE id; title is a
# free field (the model may refine the wording, but not the id). This keeps
# title drift from creating duplicate nodes between re-runs.
_MANIFEST_SYSTEM_PROMPT = """The following semantic nodes already exist (id: title):

{existing}

Assign each symbol from the list to one of the existing nodes (specify its id)
or create a new node (leave id empty). You may refine the title of an existing
node, but you MUST NOT change its id. Every symbol must be assigned to exactly
one node.

Answer STRICTLY in JSON, without markdown fences (```), without preamble. Format:

{{"nodes": [{{"id": "auth", "title": "Authentication", "symbols": ["..."]}},
            {{"id": "", "title": "New node", "symbols": ["..."]}}]}}
"""


def _manifest_system_prompt(existing: list) -> str:
    lines = "\n".join(f"- {n['id']}: {n['title']}" for n in existing)
    return _MANIFEST_SYSTEM_PROMPT.format(existing=lines)


# Backward compatibility: the old name, the default (5-15) prompt.
SYSTEM_PROMPT = _system_prompt()


def _build_user_prompt(symbols, edges) -> str:
    """The shared user prompt for both providers.

    Synthetic pseudo-<module> symbols are not sent as grouping units (they are
    not units of meaning — see shared.py), but edges involving them (e.g.
    ``app.<module> -> app.create_app``) stay in the edge list: they show the
    model that top-level code really touches those symbols.
    """
    real_ids = {s["id"] for s in symbols if not s.get("synthetic")}
    slim_symbols = [
        {"id": s["id"], "sig": s["sig"], "doc": s["doc"]}
        for s in symbols if s["id"] in real_ids
    ]
    slim_edges = [{"from": e["from"], "to": e["to"], "type": e["type"]}
                  for e in edges]
    payload = {"symbols": slim_symbols, "edges": slim_edges}
    # sort_keys — a byte-for-byte deterministic prefix between runs of the same
    # repo, important for DeepSeek's prefix-match cache.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}


# --- Providers --------------------------------------------------------

class LLMProvider(ABC):
    """One method: call the model and return the raw response text + usage.

    JSON parsing, markdown-fence stripping, id validation and node assembly are
    shared across all providers, see :func:`group_repo`. The provider is only
    responsible for the network call and normalizing usage into the common
    shape: ``{"input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "raw"}``.
    """

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int | None = None) -> dict:
        """Generic call: ``{"text", "usage"}`` for an arbitrary prompt.

        ``group`` is a special case (the grouping prompt); ``complete`` is
        needed for the second batching request (node merging) and future steps.
        """

    @abstractmethod
    def group(self, symbols, edges, node_count: int | None = None,
              node_min: int = 5, node_max: int = 15) -> dict:
        """Return ``{"text": <raw model response>, "usage": {...}}``.

        ``node_count`` — an exact node count instead of the ``node_min``-
        ``node_max`` range (control runs at a fixed granularity)."""


class DeepSeekProvider(LLMProvider):
    """OpenAI-compatible client, DeepSeek endpoint.

    Model — ``deepseek-v4-flash`` (1M context). ``deepseek-chat`` and
    ``deepseek-reasoner`` are disabled aliases, do not use.

    thinking is disabled explicitly (``{"type": "disabled"}``): it is on by
    default and billed as output, and a classification task does not need it.

    ``temperature=0`` — deterministic (as far as the API provider allows)
    grouping: the tool must return the same grouping across runs on the same
    repo, otherwise the node boundary "flickers" from run to run for no reason.
    """

    name = "deepseek"

    def __init__(self, api_key: str | None = None, model: str = DEEPSEEK_MODEL,
                 base_url: str = DEEPSEEK_BASE_URL, max_tokens: int = 32000,
                 temperature: float = 0):
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set (neither as an argument nor in env/.env)")
        from openai import OpenAI  # lazy import — don't pull the dependency unless needed
        self._client = OpenAI(api_key=key, base_url=base_url)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> dict:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or self.max_tokens,
            temperature=self.temperature,
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
                "cache_write_tokens": 0,  # DeepSeek does not bill cache writes separately
                "raw": u.model_dump() if hasattr(u, "model_dump") else dict(u),
            })
        return {"text": text, "usage": usage}

    def group(self, symbols, edges, node_count: int | None = None,
              node_min: int = 5, node_max: int = 15) -> dict:
        return self.complete(_system_prompt(node_count, node_min, node_max),
                             _build_user_prompt(symbols, edges))


class AnthropicProvider(LLMProvider):
    """Official Anthropic SDK.

    Default model — ``claude-opus-5``. For this task (a short classification of
    ~100-1500 JSON records) we set ``output_config.effort: "low"`` — it saves
    tokens on a simple task without quality loss. We do NOT disable thinking
    explicitly: disabling thinking on Opus 5 may leak a tool call or service
    tags into the visible text — there are no tools here, but the effect is the
    same for plain text; instead we use low effort, which keeps thinking (if it
    turns on at all) short.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str = ANTHROPIC_MODEL,
                 max_tokens: int = 32000):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        import anthropic  # lazy import
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> dict:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            system=system,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user}],
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

    def group(self, symbols, edges, node_count: int | None = None,
              node_min: int = 5, node_max: int = 15) -> dict:
        return self.complete(_system_prompt(node_count, node_min, node_max),
                             _build_user_prompt(symbols, edges))


PROVIDERS = {"deepseek": DeepSeekProvider, "anthropic": AnthropicProvider}


def make_provider(name: str = "deepseek", **kwargs) -> LLMProvider:
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}, available: {sorted(PROVIDERS)}")
    return PROVIDERS[name](**kwargs)


# --- Response parsing and validation (shared across providers) -----------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _dedupe_or_split_pairs(pairs):
    """``object_pairs_hook`` for json.loads: fixes a generation failure observed
    in practice — the model forgets ``}, {`` between array elements and glues
    several JSON objects into one with duplicate keys.

    A plain ``dict(pairs)`` silently keeps the last value of each duplicate key
    — of 12 nodes only one survives and the other 11 vanish without a parse
    error (the JSON is valid, just not what was written). If the object's keys
    are EXACTLY a periodic repeat of the same set (k1..kn, k1..kn, ...), we
    treat it as N glued objects and return a list of dicts instead of one dict.
    Otherwise — the normal dict behavior (last value wins, as before).
    """
    keys = [k for k, _ in pairs]
    n = len(keys)
    period = len(dict.fromkeys(keys))  # number of unique keys, in order of appearance
    if n < 2 or period == n or period == 0 or n % period != 0:
        return dict(pairs)
    cycle = keys[:period]
    if keys != cycle * (n // period):
        return dict(pairs)  # not a clean period — not our pattern, don't risk it
    return [dict(pairs[i:i + period]) for i in range(0, n, period)]


def _parse_json_response(text: str):
    """json.loads (with glued-object repair), and on failure — strip markdown
    fences and retry."""
    try:
        return json.loads(text, object_pairs_hook=_dedupe_or_split_pairs), None
    except json.JSONDecodeError:
        pass
    stripped = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(stripped, object_pairs_hook=_dedupe_or_split_pairs), None
    except json.JSONDecodeError as e:
        return None, f"failed to parse the model response as JSON: {e}"


def _validate_and_bucket(parsed, real_ids: set, warnings: list) -> list:
    """Validate the model's nodes, collect unassigned ones into UNASSIGNED_NODE.

    Returns a list of nodes of the form {"name", "summary", "symbols"}.
    """
    nodes = []
    assigned = set()

    raw_nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(raw_nodes, list):
        warnings.append("model response has no nodes list, group is empty")
        raw_nodes = []

    # _dedupe_or_split_pairs may have returned a list instead of a dict for a
    # glued element (several nodes stuck into one object) — flatten one level.
    flat_nodes = []
    for rn in raw_nodes:
        if isinstance(rn, list):
            warnings.append(f"repaired {len(rn)} model nodes glued into one JSON object")
            flat_nodes.extend(rn)
        else:
            flat_nodes.append(rn)
    raw_nodes = flat_nodes

    for rn in raw_nodes:
        if not isinstance(rn, dict) or "name" not in rn:
            warnings.append(f"skipped malformed node in model response: {rn!r}")
            continue
        ids = []
        for sid in rn.get("symbols", []):
            if sid not in real_ids:
                warnings.append(f"node {rn['name']!r}: symbol {sid!r} does not exist, dropped")
                continue
            if sid in assigned:
                warnings.append(f"symbol {sid!r} assigned to more than one node, "
                                f"kept in the first ({', '.join(n['name'] for n in nodes if sid in n['symbols']) or '?'})")
                continue
            ids.append(sid)
            assigned.add(sid)
        nodes.append({"name": rn["name"], "summary": rn.get("summary", ""), "symbols": ids})

    unassigned = sorted(real_ids - assigned)
    if unassigned:
        nodes.append({"name": UNASSIGNED_NODE,
                     "summary": "Symbols not assigned to any semantic node.",
                     "symbols": unassigned})
    return nodes


def _assign_synthetic_modules(nodes: list, symbols: list) -> None:
    """Assign pseudo-<module> symbols to the node of the majority of their
    file's real symbols.

    The LLM groups only real symbols (see _build_user_prompt); so that top-level
    code (imports, `app = create_app()`) also participates in ownership counting
    on semantic nodes, every <module> needs a node. We pick the node that owns
    the most real symbols of that file; if a file gave no real symbols to nodes
    (unlikely — then its <module> is empty too) — the UNASSIGNED_NODE.

    Mutates `nodes`: appends synthetic symbol ids to the found node.
    """
    node_by_symbol = {sid: n["name"] for n in nodes for sid in n["symbols"]}
    file_of = {s["id"]: s["file"] for s in symbols}

    file_node_counts: dict = {}
    for sid, node_name in node_by_symbol.items():
        f = file_of.get(sid)
        if f is None:
            continue
        file_node_counts.setdefault(f, {}).setdefault(node_name, 0)
        file_node_counts[f][node_name] += 1

    by_name = {n["name"]: n for n in nodes}
    for s in symbols:
        if not s.get("synthetic"):
            continue
        counts = file_node_counts.get(s["file"])
        if counts:
            winner = max(sorted(counts), key=lambda n: counts[n])
        else:
            winner = UNASSIGNED_NODE
        if winner not in by_name:
            by_name[winner] = {"name": winner, "summary": "", "symbols": []}
            nodes.append(by_name[winner])
        by_name[winner]["symbols"].append(s["id"])


def node_map(nodes: list) -> dict:
    """{"symbol_id": "node_name"} from the grouping result — for shared.py."""
    return {sid: n["name"] for n in nodes for sid in n["symbols"]}


# --- Batching (§3 of the spec) -----------------------------------------------------

_MERGE_SYSTEM_PROMPT = """Merge the code-graph nodes produced from different parts of the repository into
{count_spec} semantic nodes by PURPOSE. Nodes with a similar purpose (by name
and summary) should be merged into one; nodes with different purposes should
stay separate.

Final node names — short, in English, in domain terms.

Every node in the list must be assigned to exactly one final node.

Answer STRICTLY in JSON, without markdown fences (```), without preamble and
without explanations before or after. Response format:

{{"nodes": [{{"name": "Authentication",
            "summary": "one sentence about the purpose",
            "nodes": ["b0", "b3"]}}]}}
"""


def _merge_system_prompt(node_min: int = 5, node_max: int = 15) -> str:
    if node_min == node_max:
        count_spec = f"exactly {node_min}"
    else:
        count_spec = f"{node_min}-{node_max}"
    return _MERGE_SYSTEM_PROMPT.format(count_spec=count_spec)


def _add_usage(total: dict, usage: dict) -> None:
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens"):
        total[k] += usage.get(k, 0) or 0


def _cached(provider: LLMProvider, cache_base, system: str, user: str, call):
    """Run ``call()`` with caching keyed by (provider, model, system, user)."""
    cache_key = None
    if cache_base is not None and hasattr(provider, "name") \
            and hasattr(provider, "model"):
        cache_key = llmcache.prompt_key(provider.name, provider.model,
                                        system, user)
        cached = llmcache.load(cache_base, cache_key)
        if cached is not None:
            return cached
    result = call()
    if cache_key is not None:
        llmcache.save(cache_base, cache_key, result["text"], result["usage"],
                      provider.name, provider.model)
    return result


def _split_by_module(real_symbols: list, batch_max: int) -> list:
    """Deterministic module-based split: same input -> same batches.

    Symbols are grouped by ``file`` (module), files go in lexicographic order,
    within a file — by id. Modules are packed into batches so that the total
    does not exceed ``batch_max`` symbols; a module larger than the limit is
    split itself.
    """
    by_file: dict = {}
    for s in real_symbols:
        by_file.setdefault(s["file"], []).append(s)

    batches: list = []
    cur: list = []
    cur_count = 0
    for f in sorted(by_file):
        syms = sorted(by_file[f], key=lambda s: s["id"])
        n = len(syms)
        if n > batch_max:
            if cur:
                batches.append(cur)
                cur, cur_count = [], 0
            for i in range(0, n, batch_max):
                batches.append(syms[i:i + batch_max])
        elif cur and cur_count + n > batch_max:
            batches.append(cur)
            cur, cur_count = list(syms), n
        else:
            cur.extend(syms)
            cur_count += n
    if cur:
        batches.append(cur)
    return batches


def _merge_by_name(batch_nodes: list) -> list:
    """Deterministic merge without the LLM: identical names -> one node."""
    groups: dict = {}
    for bn in batch_nodes:
        groups.setdefault(bn["name"], []).append(bn)
    final = []
    for name, members in sorted(groups.items()):
        symbols = []
        for m in members:
            symbols.extend(m["symbols"])
        final.append({"name": name, "summary": members[0]["summary"],
                      "symbols": symbols})
    return final


def _finalize_merge(parsed, batch_nodes: list, warnings: list) -> list | None:
    """Merge response -> final nodes; None if the structure is invalid."""
    ref_by_node = {bn["ref"]: bn for bn in batch_nodes}
    valid_refs = set(ref_by_node)
    raw = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return None

    final = []
    assigned = set()
    for rn in raw:
        if not isinstance(rn, dict) or "name" not in rn:
            warnings.append(f"skipped malformed final merge node: {rn!r}")
            continue
        refs = [r for r in rn.get("nodes", [])
                if r in valid_refs and r not in assigned]
        # guard against duplicate refs in one response (the model may repeat)
        refs = list(dict.fromkeys(refs))
        if not refs:
            continue
        symbols = []
        for r in refs:
            assigned.add(r)
            symbols.extend(ref_by_node[r]["symbols"])
        final.append({"name": rn["name"], "summary": rn.get("summary", ""),
                      "symbols": symbols})

    for r in sorted(valid_refs - assigned):
        bn = ref_by_node[r]
        final.append({"name": bn["name"], "summary": bn["summary"],
                      "symbols": list(bn["symbols"])})
    return final


def _merge_nodes(batch_nodes: list, provider: LLMProvider, cache_base,
                 node_min: int = 5, node_max: int = 15):
    """Second request: merge the batch nodes. On failure -> deterministic
    name-based merge."""
    system = _merge_system_prompt(node_min, node_max)
    user = json.dumps(
        {"nodes": [{"ref": bn["ref"], "name": bn["name"],
                    "summary": bn["summary"], "count": len(bn["symbols"])}
                   for bn in batch_nodes]},
        ensure_ascii=False, sort_keys=True)
    warnings = []

    try:
        result = _cached(provider, cache_base, system, user,
                         lambda: provider.complete(system, user, max_tokens=2000))
    except Exception as e:
        warnings.append(f"batch merge failed ({e}); merging by name")
        return _merge_by_name(batch_nodes), warnings, _empty_usage(), ""

    parsed, err = _parse_json_response(result["text"])
    if err:
        warnings.append(f"batch merge: {err}; merging by name")
        return _merge_by_name(batch_nodes), warnings, result["usage"], result["text"]

    final = _finalize_merge(parsed, batch_nodes, warnings)
    if final is None:
        warnings.append("merge response has no nodes list; merging by name")
        final = _merge_by_name(batch_nodes)
    return final, warnings, result["usage"], result["text"]


def _group_single(symbols, edges, provider: LLMProvider, node_count, cache_base,
                  real_ids: set, node_min: int, node_max: int):
    """A single grouping call (no batching) -> (nodes, warnings, usage, raw_text)."""
    system = _system_prompt(node_count, node_min, node_max)
    user = _build_user_prompt(symbols, edges)
    result = _cached(provider, cache_base, system, user,
                     lambda: provider.group(symbols, edges, node_count=node_count,
                                            node_min=node_min, node_max=node_max))
    warnings = []
    parsed, err = _parse_json_response(result["text"])
    if err:
        warnings.append(err)
        parsed = {"nodes": []}
    nodes = _validate_and_bucket(parsed, real_ids, warnings)
    return nodes, warnings, result["usage"], result["text"]


def _group_batched(symbols, edges, real: list, provider: LLMProvider, node_count,
                   cache_base, batch_max: int, node_min: int, node_max: int):
    """Batching: split, group each batch, merge with a second request."""
    batches = _split_by_module(real, batch_max)
    warnings = []
    batch_nodes = []
    total_usage = _empty_usage()
    raw_texts = []
    ref_counter = 0

    for i, batch in enumerate(batches):
        log.info("grouping: batch %d/%d (%d symbols)",
                 i + 1, len(batches), len(batch))
        batch_ids = {s["id"] for s in batch}
        batch_edges = [e for e in edges
                       if e["from"] in batch_ids and e["to"] in batch_ids]
        system = _system_prompt(node_count, node_min, node_max)
        user = _build_user_prompt(batch, batch_edges)
        try:
            result = _cached(
                provider, cache_base, system, user,
                lambda b=batch, be=batch_edges: provider.group(
                    b, be, node_count=node_count,
                    node_min=node_min, node_max=node_max))
        except Exception as e:
            warnings.append(f"batch {i + 1}/{len(batches)} not grouped "
                            f"({e}); its symbols go to {UNASSIGNED_NODE}")
            continue
        parsed, err = _parse_json_response(result["text"])
        if err:
            warnings.append(f"batch {i + 1}/{len(batches)}: {err}; "
                            f"its symbols go to {UNASSIGNED_NODE}")
            continue
        bnodes = _validate_and_bucket(parsed, batch_ids, warnings)
        for bn in bnodes:
            batch_nodes.append({"ref": f"n{ref_counter}", "name": bn["name"],
                                "summary": bn.get("summary", ""),
                                "symbols": bn["symbols"]})
            ref_counter += 1
        _add_usage(total_usage, result["usage"])
        raw_texts.append(result["text"])

    # Symbols of failed batches (partial failure) also go to UNASSIGNED_NODE.
    assigned = {sid for bn in batch_nodes for sid in bn["symbols"]}
    missing = {s["id"] for s in real} - assigned

    # "Unassigned" (failures/unassigned) is NOT merged by the second request —
    # it's a service catch-all node, it always stays separate.
    merge_nodes = [bn for bn in batch_nodes if bn["name"] != UNASSIGNED_NODE]
    unassigned = sorted({sid for bn in batch_nodes
                         if bn["name"] == UNASSIGNED_NODE for sid in bn["symbols"]}
                        | missing)

    if len(merge_nodes) > 1:
        nodes, mwarnings, musage, mtext = _merge_nodes(merge_nodes, provider,
                                                       cache_base,
                                                       node_min, node_max)
        warnings.extend(mwarnings)
        _add_usage(total_usage, musage)
        raw_texts.append(mtext)
    else:
        nodes = [{"name": bn["name"], "summary": bn["summary"],
                  "symbols": list(bn["symbols"])} for bn in merge_nodes]

    if unassigned:
        nodes.append({"name": UNASSIGNED_NODE,
                      "summary": "Symbols not assigned to any semantic node.",
                      "symbols": unassigned})

    return nodes, warnings, total_usage, "\n\n".join(raw_texts)


def _parse_manifest_response(parsed, free_ids: set, existing_ids: set,
                             warnings: list):
    """Parse the manifest-mode response.

    Returns ``(merged, new_nodes, unassigned)``:
    * ``merged`` — ``{id: {title, symbols}}`` assignment to EXISTING nodes
      (by stable id; title drift does not create duplicates);
    * ``new_nodes`` — ``{title: [symbols]}`` new nodes;
    * ``unassigned`` — free symbols assigned nowhere.
    """
    raw = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        warnings.append("model response has no nodes list")
        raw = []

    merged: dict = {}
    new_nodes: dict = {}
    assigned = set()
    for rn in raw:
        if not isinstance(rn, dict):
            continue
        nid = rn.get("id")
        title = rn.get("title") or rn.get("name") or ""
        valid = [s for s in rn.get("symbols", [])
                 if s in free_ids and s not in assigned]
        for s in valid:
            assigned.add(s)
        if nid and nid in existing_ids:
            e = merged.setdefault(nid, {"title": title, "symbols": []})
            e["title"] = title
            e["symbols"].extend(valid)
        elif title:
            new_nodes.setdefault(title, []).extend(valid)

    unassigned = sorted(free_ids - assigned)
    return merged, new_nodes, unassigned


def _group_pinned(symbols, edges, real: list, provider: LLMProvider,
                  cache_base, node_count, node_min, node_max,
                  pinned: dict, existing_nodes: list) -> tuple:
    """Manifest mode: some symbols are already assigned (``pinned``, by id); the
    model classifies ONLY the free symbols — into existing nodes (by id) or new
    ones. Existing nodes are not renamed or split; title is a free field (the
    model may refine it, the id stays).
    """
    warnings: list = []
    usage = _empty_usage()
    raw_texts: list = []

    id2title = {n["id"]: n["title"] for n in existing_nodes}

    by_id: dict = {}
    for sid, nid in pinned.items():
        by_id.setdefault(nid, []).append(sid)

    nodes: list = []
    for nid, sids in sorted(by_id.items()):
        nodes.append({"name": id2title.get(nid, nid), "id": nid, "summary": "",
                      "symbols": sorted(sids)})

    free = [s for s in real if s["id"] not in pinned]
    if free:
        free_ids = {s["id"] for s in free}
        free_edges = [e for e in edges
                      if e["from"] in free_ids and e["to"] in free_ids]
        system = _manifest_system_prompt(existing_nodes)
        user = _build_user_prompt(free, free_edges)
        result = _cached(provider, cache_base, system, user,
                         lambda: provider.complete(system, user, max_tokens=32000))
        usage = result["usage"]
        raw_texts.append(result["text"])
        parsed, err = _parse_json_response(result["text"])
        if err:
            warnings.append(err)
            parsed = {"nodes": []}
        merged, new_nodes, unassigned = _parse_manifest_response(
            parsed, free_ids, set(id2title), warnings)

        # Assign free symbols to existing nodes by id.
        for nid, m in merged.items():
            target = next((n for n in nodes if n.get("id") == nid), None)
            if target is None:
                target = {"name": m["title"], "id": nid, "summary": "",
                          "symbols": []}
                nodes.append(target)
            target["name"] = m["title"]
            target["symbols"].extend(m["symbols"])
        for title, syms in new_nodes.items():
            nodes.append({"name": title, "summary": "",
                          "symbols": sorted(syms)})
        if unassigned:
            nodes.append({"name": UNASSIGNED_NODE,
                          "summary": "Symbols not assigned to any semantic node.",
                          "symbols": unassigned})

    for n in nodes:
        n["symbols"] = sorted(set(n["symbols"]))

    unassigned_count = len(next(
        (n["symbols"] for n in nodes if n["name"] == UNASSIGNED_NODE), []))
    _assign_synthetic_modules(nodes, symbols)
    return nodes, warnings, usage, "\n\n".join(raw_texts), unassigned_count


def group_repo(symbols, edges, provider: LLMProvider, node_count: int | None = None,
               cache_base=None, batch_max: int = BATCH_MAX_SYMBOLS,
               node_min: int = 5, node_max: int = 15,
               pinned: dict | None = None, existing_nodes: list | None = None) -> dict:
    """Stage B orchestration: grouping (with batching per §3) + validation.

    ``node_count`` — pass an exact K instead of the ``node_min``-``node_max``
    range (control runs at a fixed granularity); ``node_min``/``node_max`` come
    from config and set the node range.

    ``cache_base`` — the LLM-response cache directory (see llmcache.py). When
    set and the provider carries ``name``/``model`` attributes, the response is
    cached by prompt hash and a repeated call on the same graph does not hit the
    API.

    ``batch_max`` — the batching threshold: more real symbols than the limit ->
    split by module + a second merge request (§3). Same input always yields the
    same batches (determinism); a partial batch failure does not crash the run.

    ``pinned``/``existing_nodes`` — manifest mode (see :func:`_group_pinned`):
    ``pinned`` = {symbol_id: node_name} of pre-assigned symbols; the model
    classifies only the free ones and does not rename existing nodes.

    Returns ``{"nodes": [...], "unassigned_count", "warnings", "usage",
    "raw_text"}``. ``nodes`` already includes the synthetic <module> symbols
    (see :func:`_assign_synthetic_modules`) — ready for :func:`node_map`.
    ``raw_text`` — the raw model response(s), for saving/debugging.
    """
    real = [s for s in symbols if not s.get("synthetic")]
    real_ids = {s["id"] for s in real}

    if pinned:
        nodes, warnings, usage, raw_text, unassigned_count = _group_pinned(
            symbols, edges, real, provider, cache_base, node_count,
            node_min, node_max, pinned, existing_nodes or [])
        return {
            "nodes": nodes, "unassigned_count": unassigned_count,
            "warnings": warnings, "usage": usage, "raw_text": raw_text,
        }

    if len(real) <= batch_max:
        nodes, warnings, usage, raw_text = _group_single(
            symbols, edges, provider, node_count, cache_base, real_ids,
            node_min, node_max)
    else:
        log.info("batching: %d symbols into %d batches (limit %d)",
                 len(real), len(_split_by_module(real, batch_max)), batch_max)
        nodes, warnings, usage, raw_text = _group_batched(
            symbols, edges, real, provider, node_count, cache_base, batch_max,
            node_min, node_max)

    # Grouping quality indicator — counted BEFORE assigning synthetic <module>
    # symbols: those land in UNASSIGNED_NODE via majority vote (see
    # _assign_synthetic_modules), not because the model failed to group them
    # (they were never shown to it — see _build_user_prompt).
    unassigned_count = len(next(
        (n["symbols"] for n in nodes if n["name"] == UNASSIGNED_NODE), []))

    _assign_synthetic_modules(nodes, symbols)

    return {
        "nodes": nodes,
        "unassigned_count": unassigned_count,
        "warnings": warnings,
        "usage": usage,
        "raw_text": raw_text,
    }
