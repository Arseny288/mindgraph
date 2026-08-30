"""Тесты прогресса/--verbose (фаза 0.6).

Лог этапов идёт ТОЛЬКО в stderr; stdout остаётся чистым (важно для JSON-контракта
фазы 1). Батчинг логирует «батч i/N».
"""

import json
import logging
import os
import subprocess
import sys

import pytest

from mindgraph.extract import extract_repo
from mindgraph.group import LLMProvider, group_repo

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


class _FakeProvider(LLMProvider):
    name = "deepseek"
    model = "test"

    def group(self, symbols, edges, node_count=None, node_min=5, node_max=15):
        ids = [s["id"] for s in symbols]
        return {"text": json.dumps({"nodes": [{"name": "Всё", "summary": "",
                                               "symbols": ids}]}),
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_tokens": 0, "cache_write_tokens": 0,
                          "raw": {}}}

    def complete(self, system, user, max_tokens=None):
        return {"text": "{}", "usage": {"input_tokens": 1, "output_tokens": 1,
                                        "cache_read_tokens": 0,
                                        "cache_write_tokens": 0, "raw": {}}}


def test_batch_progress_logged(caplog):
    g = extract_repo(FIXTURE)
    with caplog.at_level(logging.INFO, logger="mindgraph.group"):
        group_repo(g["symbols"], g["symbol_edges"], _FakeProvider(),
                   batch_max=5)
    assert any("батч" in r.message for r in caplog.records)
    assert any("/" in r.message and "символов" in r.message
               for r in caplog.records)


def test_cli_verbose_logs_to_stderr_and_stdout_stays_clean():
    proc = subprocess.run(
        [sys.executable, "-m", "mindgraph.cli", "scan", FIXTURE,
         "--no-llm", "--force", "--verbose"],
        capture_output=True, text=True)
    assert proc.returncode == 0
    assert "этап A" in proc.stderr
    assert proc.stdout == ""
