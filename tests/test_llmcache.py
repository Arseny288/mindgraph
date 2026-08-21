"""Тесты LLM-кэша (llmcache.py) и его интеграции в group_repo.

Проверяем: ключ стабилен и чувствителен к каждой части промпта; запись/чтение
round-trip; попадание в кэш не вызывает провайдера (деньги не тратятся).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph import llmcache
from mindgraph.extract import extract_repo
from mindgraph.group import LLMProvider, group_repo

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


def test_prompt_key_stable_and_sensitive():
    k1 = llmcache.prompt_key("deepseek", "m", "sys", "user")
    k2 = llmcache.prompt_key("deepseek", "m", "sys", "user")
    assert k1 == k2
    for other in [
        llmcache.prompt_key("anthropic", "m", "sys", "user"),
        llmcache.prompt_key("deepseek", "m2", "sys", "user"),
        llmcache.prompt_key("deepseek", "m", "sys2", "user"),
        llmcache.prompt_key("deepseek", "m", "sys", "user2"),
    ]:
        assert other != k1


def test_save_load_roundtrip():
    base = Path(tempfile.mkdtemp())
    k = llmcache.prompt_key("deepseek", "m", "s", "u")
    llmcache.save(base, k, "hello", {"input_tokens": 5}, "deepseek", "m")
    hit = llmcache.load(base, k)
    assert hit == {"text": "hello", "usage": {"input_tokens": 5}}
    assert llmcache.load(base, "0" * 64) is None


def test_load_missing_or_corrupt_returns_none():
    base = Path(tempfile.mkdtemp())
    assert llmcache.load(base, "a" * 64) is None
    (base / f"{'b' * 64}.json").write_text("{not json", encoding="utf-8")
    assert llmcache.load(base, "b" * 64) is None


class _CachingProvider(LLMProvider):
    name = "deepseek"
    model = "test-model"

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def group(self, symbols, edges, node_count=None):
        self.calls += 1
        return {"text": self.text, "usage": {"input_tokens": 1, "output_tokens": 1,
                                             "cache_read_tokens": 0, "cache_write_tokens": 0,
                                             "raw": {}}}


def test_group_repo_cache_hit_skips_provider_call():
    g = extract_repo(FIXTURE)
    base = Path(tempfile.mkdtemp())
    text = '{"nodes": []}'  # всё в «Прочее» — валидный ответ, кэшируется как текст

    p1 = _CachingProvider(text)
    r1 = group_repo(g["symbols"], g["symbol_edges"], p1, cache_base=base)
    assert p1.calls == 1

    # Второй прогон с ДРУГИМ провайдером (та же модель/промпт): должен взять
    # ответ из кэша и не вызвать API ни разу.
    p2 = _CachingProvider(text)
    r2 = group_repo(g["symbols"], g["symbol_edges"], p2, cache_base=base)
    assert p2.calls == 0
    assert r2["raw_text"] == r1["raw_text"] == text


def test_group_repo_no_cache_base_still_calls():
    g = extract_repo(FIXTURE)
    p = _CachingProvider('{"nodes": []}')
    group_repo(g["symbols"], g["symbol_edges"], p)  # без cache_base
    assert p.calls == 1


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
