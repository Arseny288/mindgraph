"""llmcache.py — кэш LLM-ответов по хэшу промпта.

Ключ = ``sha256(provider, model, system_prompt, user_prompt)``. Промпты
mindgraph детерминированы (``sort_keys=True`` в JSON-пейлоадах), поэтому один
и тот же (провайдер, модель, граф) всегда даёт один ключ. Повторный прогон —
в т.ч. платный ``--regroup`` на неизменном коде — берёт ответ из кэша и не
ходит в API.

Хранение: файл ``<key>.json`` в ``<cache_base>/llm/``. По умолчанию база —
``~/.cache/mindgraph`` (см. ``mindgraph/config.py``); расширение ``~`` делает
:func:`resolve_base`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

DEFAULT_CACHE_DIR = "~/.cache/mindgraph"


def resolve_base(cache_dir: str | os.PathLike | None = None) -> Path:
    """Каталог кэша LLM-ответов: ``expanduser(cache_dir)/llm``."""
    d = os.path.expanduser(os.fspath(cache_dir or DEFAULT_CACHE_DIR))
    return Path(d) / "llm"


def prompt_key(provider: str, model: str, system: str, user: str) -> str:
    """Стабильный ключ по (провайдер, модель, system-промпт, user-промпт)."""
    h = hashlib.sha256()
    for part in (provider, model or "", system, user):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load(base: Path, key: str) -> dict | None:
    """Вернуть ``{"text", "usage"}`` из кэша, либо None (нет/битая запись)."""
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
    """Атомарно записать ответ в ``<base>/<key>.json``."""
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
