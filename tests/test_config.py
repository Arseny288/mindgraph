"""Config resolution tests (config.py): defaults, env, TOML, priorities."""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.config import Config, resolve_config


def test_defaults():
    c = resolve_config("/nonexistent-repo")
    assert c.provider == "deepseek"
    assert c.model is None
    assert c.node_min == 5 and c.node_max == 15
    assert c.port == 5173
    assert c.cache_dir == "~/.cache/mindgraph"
    assert c.no_llm is False


def test_env_overrides_defaults():
    os.environ["MINDGRAPH_PORT"] = "9999"
    os.environ["MINDGRAPH_PROVIDER"] = "anthropic"
    try:
        c = resolve_config("/nonexistent-repo")
        assert c.port == 9999
        assert c.provider == "anthropic"
    finally:
        del os.environ["MINDGRAPH_PORT"]
        del os.environ["MINDGRAPH_PROVIDER"]


def test_repo_toml_overrides_defaults():
    root = tempfile.mkdtemp(prefix="mindgraph_cfg_")
    with open(os.path.join(root, "mindgraph.toml"), "w", encoding="utf-8") as fh:
        fh.write('[llm]\nprovider = "anthropic"\nnode_max = 12\n'
                 '[scan]\nignore = ["tests", "docs"]\n'
                 '[serve]\nport = 8080\n'
                 '[cache]\ndir = "/tmp/mgcache"\n')
    try:
        c = resolve_config(root)
        assert c.provider == "anthropic"
        assert c.node_max == 12
        assert c.ignore == ["tests", "docs"]
        assert c.port == 8080
        assert c.cache_dir == "/tmp/mgcache"
    finally:
        shutil.rmtree(root)


def test_env_beats_repo_toml():
    root = tempfile.mkdtemp(prefix="mindgraph_cfg_")
    with open(os.path.join(root, "mindgraph.toml"), "w", encoding="utf-8") as fh:
        fh.write('[llm]\nprovider = "anthropic"\n')
    os.environ["MINDGRAPH_PROVIDER"] = "deepseek"
    try:
        c = resolve_config(root)
        assert c.provider == "deepseek"
    finally:
        del os.environ["MINDGRAPH_PROVIDER"]
        shutil.rmtree(root)


def test_explicit_config_path_replaces_repo_toml():
    root = tempfile.mkdtemp(prefix="mindgraph_cfg_")
    with open(os.path.join(root, "mindgraph.toml"), "w", encoding="utf-8") as fh:
        fh.write('[llm]\nprovider = "anthropic"\n')
    alt = os.path.join(root, "other.toml")
    with open(alt, "w", encoding="utf-8") as fh:
        fh.write('[llm]\nprovider = "deepseek"\n')
    try:
        c = resolve_config(root, explicit_path=alt)
        assert c.provider == "deepseek"
    finally:
        shutil.rmtree(root)


def test_from_dict_drops_unknown_keys():
    c = Config.from_dict({"provider": "anthropic", "bogus": 123})
    assert c.provider == "anthropic"
    assert not hasattr(c, "bogus")


def test_broken_toml_does_not_crash():
    root = tempfile.mkdtemp(prefix="mindgraph_cfg_")
    with open(os.path.join(root, "mindgraph.toml"), "w", encoding="utf-8") as fh:
        fh.write("this is not toml = [")
    try:
        c = resolve_config(root)  # does not crash, returns defaults
        assert c.provider == "deepseek"
    finally:
        shutil.rmtree(root)


def _run():
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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
