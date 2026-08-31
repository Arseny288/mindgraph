"""config.py — mindgraph settings and their resolution.

Priority (later wins):

1. defaults (fields of :class:`Config`);
2. user-level ``~/.config/mindgraph/config.toml``;
3. repo-local ``<repo>/mindgraph.toml``;
4. ``MINDGRAPH_*`` environment variables;
5. explicit CLI flags (applied in cli.py after :func:`resolve_config`).

TOML format (see MINDGRAPH.md):

    [llm]   provider / model / node_min / node_max / no_llm
    [scan]  ignore = [...] / entry_points = [...]
    [serve] port / open_browser
    [cache] dir
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .shared import ENTRY_POINT_PATTERNS

# Name of the repo-local config file and the user config directory.
REPO_CONFIG_NAME = "mindgraph.toml"
USER_CONFIG_DIR = Path("~/.config/mindgraph").expanduser()
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.toml"

# Default directory for LLM-response cache (expanduser is applied by the consumer).
DEFAULT_CACHE_DIR = "~/.cache/mindgraph"


@dataclass
class Config:
    """Tool settings. Fields = keys of the TOML sections, see module docstring."""

    provider: str = "deepseek"
    model: str | None = None  # None -> the provider's default model
    node_min: int = 5
    node_max: int = 15
    no_llm: bool = False
    ignore: list = field(default_factory=list)
    entry_points: list = field(default_factory=lambda: list(ENTRY_POINT_PATTERNS))
    cache_dir: str = DEFAULT_CACHE_DIR
    port: int = 5173
    open_browser: bool = True

    # --- resolution ------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _user_config_path() -> Path | None:
    return USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else None


def _repo_config_path(repo_root: str) -> Path | None:
    p = Path(repo_root) / REPO_CONFIG_NAME
    return p if p.exists() else None


def _load_toml(path: Path) -> dict:
    """Read the TOML and flatten the ``[llm]``/``[scan]``/``[serve]``/
    ``[cache]`` sections into a flat dict of Config fields. Never raises: a
    broken config is a stderr warning and an empty dict."""
    import sys
    import tomllib

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"mindgraph: config {path} could not be read ({e})", file=sys.stderr)
        return {}

    flat: dict = {}
    llm = raw.get("llm", {}) if isinstance(raw.get("llm"), dict) else {}
    scan = raw.get("scan", {}) if isinstance(raw.get("scan"), dict) else {}
    serve = raw.get("serve", {}) if isinstance(raw.get("serve"), dict) else {}
    cache = raw.get("cache", {}) if isinstance(raw.get("cache"), dict) else {}

    mapping = {
        "provider": ("llm", "provider"),
        "model": ("llm", "model"),
        "node_min": ("llm", "node_min"),
        "node_max": ("llm", "node_max"),
        "no_llm": ("llm", "no_llm"),
        "ignore": ("scan", "ignore"),
        "entry_points": ("scan", "entry_points"),
        "port": ("serve", "port"),
        "open_browser": ("serve", "open_browser"),
        "cache_dir": ("cache", "dir"),
    }
    sections = {"llm": llm, "scan": scan, "serve": serve, "cache": cache}
    for field_name, (section, key) in mapping.items():
        if key in sections[section]:
            flat[field_name] = sections[section][key]
    return flat


def _env_overrides() -> dict:
    """``MINDGRAPH_*`` environment variables -> a flat dict of fields."""
    env = os.environ
    out: dict = {}
    if env.get("MINDGRAPH_PROVIDER"):
        out["provider"] = env["MINDGRAPH_PROVIDER"]
    if env.get("MINDGRAPH_MODEL"):
        out["model"] = env["MINDGRAPH_MODEL"]
    if env.get("MINDGRAPH_NODE_MIN"):
        out["node_min"] = int(env["MINDGRAPH_NODE_MIN"])
    if env.get("MINDGRAPH_NODE_MAX"):
        out["node_max"] = int(env["MINDGRAPH_NODE_MAX"])
    if env.get("MINDGRAPH_CACHE_DIR"):
        out["cache_dir"] = env["MINDGRAPH_CACHE_DIR"]
    if env.get("MINDGRAPH_PORT"):
        out["port"] = int(env["MINDGRAPH_PORT"])
    if env.get("MINDGRAPH_IGNORE"):
        out["ignore"] = [s.strip() for s in env["MINDGRAPH_IGNORE"].split(",") if s.strip()]
    if env.get("MINDGRAPH_NO_LLM"):
        out["no_llm"] = env["MINDGRAPH_NO_LLM"].lower() in ("1", "true", "yes", "on")
    if env.get("MINDGRAPH_OPEN_BROWSER"):
        out["open_browser"] = env["MINDGRAPH_OPEN_BROWSER"].lower() in ("1", "true", "yes", "on")
    return out


def resolve_config(repo_root: str, explicit_path: str | None = None) -> Config:
    """Resolve the config: defaults <- user <- repo <- env <- (CLI later).

    ``explicit_path`` — ``--config``; when set, EXACTLY this file is used
    instead of the user-level and repo-local ones.
    """
    flat: dict = {}

    if explicit_path:
        flat.update(_load_toml(Path(explicit_path)))
    else:
        user = _user_config_path()
        if user:
            flat.update(_load_toml(user))
        repo = _repo_config_path(repo_root)
        if repo:
            flat.update(_load_toml(repo))

    flat.update(_env_overrides())
    return Config.from_dict(flat)
