"""llmcache.py — LLM response cache keyed by prompt hash.

Key = ``sha256(provider, model, system_prompt, user_prompt)``. mindgraph's
prompts are deterministic (``sort_keys=True`` in JSON payloads), so the same
(provider, model, graph) always yields the same key. A repeated run — including
a paid ``--regroup`` on unchanged code — takes the answer from the cache and
does not hit the API.

Storage: a ``<key>.json`` file in ``<cache_base>/llm/``. The default base is
``~/.cache/mindgraph`` (see ``mindgraph/config.py``); the ``~`` expansion is
done by :func:`resolve_base`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

DEFAULT_CACHE_DIR = "~/.cache/mindgraph"


def resolve_base(cache_dir: str | os.PathLike | None = None) -> Path:
    """LLM-response cache directory: ``expanduser(cache_dir)/llm``."""
    d = os.path.expanduser(os.fspath(cache_dir or DEFAULT_CACHE_DIR))
    return Path(d) / "llm"


def prompt_key(provider: str, model: str, system: str, user: str) -> str:
    """Stable key from (provider, model, system prompt, user prompt)."""
    h = hashlib.sha256()
    for part in (provider, model or "", system, user):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load(base: Path, key: str) -> dict | None:
    """Return ``{"text", "usage"}`` from the cache, or None (missing/corrupt)."""
    path = base / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "text" not in data:
        return None
    return {"text": data["text"], "usage": data.get("usage") or {}}


def save(base: Path, key: str, text: str, usage: dict,
         provider: str, model: str) -> None:
    """Atomically write the response to ``<base>/<key>.json``."""
    base.mkdir(parents=True, exist_ok=True)
    entry = {
        "key": key,
        "provider": provider,
        "model": model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "text": text,
        "usage": usage,
    }
    tmp = base / f"{key}.json.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(base / f"{key}.json")
