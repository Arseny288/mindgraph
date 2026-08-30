"""Тесты батчинга группировки (фаза 0.5).

Без сети: фейковый провайдер. Проверяем детерминированное разбиение по
модулям, сам путь «батчи + слияние вторым запросом», и частичный отказ батча
(символы упавшего батча уходят в «Прочее», остальное продолжает работать).
"""

import json
import os

import pytest

from mindgraph.extract import extract_repo
from mindgraph.group import (LLMProvider, UNASSIGNED_NODE, group_repo,
                             _split_by_module)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")

_USAGE = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
          "cache_write_tokens": 0, "raw": {}}


class _BatchingProvider(LLMProvider):
    """group: символы -> узлы по файлу (имя 'pkg:<file>'); complete: слияние."""

    name = "deepseek"
    model = "test-model"

    def __init__(self, merge="all", fail_when=None):
        self.merge = merge
        self.fail_when = fail_when
        self.group_calls = 0
        self.complete_calls = 0

    def group(self, symbols, edges, node_count=None):
        self.group_calls += 1
        if self.fail_when and any(self.fail_when(s["id"]) for s in symbols):
            return {"text": "не json", "usage": _USAGE}
        by_file = {}
        for s in symbols:
            by_file.setdefault(s["file"], []).append(s["id"])
        nodes = [{"name": f"pkg:{f}", "summary": "", "symbols": sorted(ids)}
                 for f, ids in sorted(by_file.items())]
        return {"text": json.dumps({"nodes": nodes}, ensure_ascii=False),
                "usage": _USAGE}

    def complete(self, system, user, max_tokens=None):
        self.complete_calls += 1
        if self.merge == "invalid":
            return {"text": "не json", "usage": _USAGE}
        if self.merge == "all":
            refs = [n["ref"] for n in json.loads(user)["nodes"]]
            return {"text": json.dumps(
                {"nodes": [{"name": "Всё", "summary": "", "nodes": refs}]},
                ensure_ascii=False), "usage": _USAGE}
        # merge="none": оставить узлы как есть (по одному итоговому узлу на ref)
        refs = [n["ref"] for n in json.loads(user)["nodes"]]
        return {"text": json.dumps(
            {"nodes": [{"name": f"m_{r}", "summary": "", "nodes": [r]}
                       for r in refs]}, ensure_ascii=False), "usage": _USAGE}


def _real_count(g):
    return len([s for s in g["symbols"] if not s.get("synthetic")])


def test_split_by_module_deterministic_and_within_limit():
    g = extract_repo(FIXTURE)
    real = [s for s in g["symbols"] if not s.get("synthetic")]
    b1 = _split_by_module(real, 5)
    b2 = _split_by_module(real, 5)
    assert b1 == b2  # детерминизм
    assert all(len(b) <= 5 for b in b1)
    assert sum(len(b) for b in b1) == len(real)
    assert len(b1) > 1  # 13+ реальных символов при лимите 5 дают несколько батчей


def test_group_repo_batches_and_merges():
    g = extract_repo(FIXTURE)
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}

    p = _BatchingProvider(merge="all")
    result = group_repo(g["symbols"], g["symbol_edges"], p, batch_max=5)

    assert p.group_calls > 1       # реально шли батчами
    assert p.complete_calls == 1   # и был второй запрос слияния

    from mindgraph.group import node_map
    m = node_map(result["nodes"])
    # каждый реальный символ назначен (слияние "Всё" собрало все батчи)
    assert set(real_ids) <= set(m)
    for sid in real_ids:
        assert m[sid] == "Всё"


def test_group_repo_batching_deterministic_across_runs():
    g = extract_repo(FIXTURE)
    real_ids = sorted(s["id"] for s in g["symbols"] if not s.get("synthetic"))

    r1 = group_repo(g["symbols"], g["symbol_edges"], _BatchingProvider(), batch_max=5)
    r2 = group_repo(g["symbols"], g["symbol_edges"], _BatchingProvider(), batch_max=5)

    from mindgraph.group import node_map
    m1, m2 = node_map(r1["nodes"]), node_map(r2["nodes"])
    assert {sid: m1[sid] for sid in real_ids} == {sid: m2[sid] for sid in real_ids}


def test_partial_batch_failure_is_not_fatal():
    g = extract_repo(FIXTURE)
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}

    # Падает батч, содержащий cycle.*; вместе с ним в том же батче лежат
    # common.utils.* — их символы тоже уходят в «Прочее». Остальные
    # группируются и сливаются в «Всё» как обычно.
    p = _BatchingProvider(merge="all", fail_when=lambda sid: sid.startswith("cycle."))
    result = group_repo(g["symbols"], g["symbol_edges"], p, batch_max=5)

    from mindgraph.group import node_map
    m = node_map(result["nodes"])

    failed = {sid for sid in real_ids
              if sid.startswith("cycle.") or sid.startswith("common.utils.")}
    ok_ids = real_ids - failed

    for sid in failed:
        assert m[sid] == UNASSIGNED_NODE
    for sid in ok_ids:
        assert m[sid] == "Всё"
    assert any("батч" in w for w in result["warnings"])
