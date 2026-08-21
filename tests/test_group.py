"""Тесты этапа B (LLM-группировка) на фикстуре — без сети и без ключей.

Парсинг/валидация/бакетинг/приписывание псевдо-<module> — чистые функции,
не зависящие от конкретного провайдера. Тестируем их через FakeProvider,
который возвращает заранее заданный текст вместо похода в API.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.extract import extract_repo
from mindgraph.group import (LLMProvider, UNASSIGNED_NODE, group_repo, node_map,
                            _parse_json_response, _validate_and_bucket,
                            _assign_synthetic_modules)
from mindgraph.shared import compute_owners_by_node, summarize_by_node

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")

# Разбивка: авторизация + биллинг реально сгруппированы, common разнесён
# (hash_value/CACHE — в "Общее", normalize оставлен без узла намеренно),
# cycle.* тоже оставлен без узла — три символа должны уйти в UNASSIGNED_NODE.
FAKE_RESPONSE = {
    "nodes": [
        {"name": "Аутентификация", "summary": "Вход и токены.",
         "symbols": ["auth.tokens.issue", "auth.tokens.refresh",
                    "auth.login.verify_token", "auth.login.login"]},
        {"name": "Биллинг", "summary": "Счета и налоги.",
         "symbols": ["billing.invoice.Invoice", "billing.invoice.Invoice.total",
                    "billing.invoice.Invoice.signature", "billing.invoice.Invoice.render",
                    "billing.invoice.TaxInvoice", "billing.invoice.TaxInvoice.total",
                    "billing.invoice.make_invoice",
                    "nonexistent.symbol"]},  # невалидный id — должен быть отброшен
        {"name": "Общее", "summary": "Общая инфраструктура.",
         "symbols": ["common.utils.hash_value", "common.utils.CACHE",
                    "auth.tokens.issue"]},  # дубль issue — должен остаться в первом узле
    ]
}


class FakeProvider(LLMProvider):
    def __init__(self, text=None, usage=None):
        self.text = text if text is not None else json.dumps(FAKE_RESPONSE, ensure_ascii=False)
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50,
                               "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}
        self.calls = []

    def group(self, symbols, edges, node_count=None):
        self.calls.append((symbols, edges, node_count))
        return {"text": self.text, "usage": self.usage}


def _graph():
    return extract_repo(FIXTURE)


def test_parse_json_plain():
    parsed, err = _parse_json_response('{"nodes": []}')
    assert err is None
    assert parsed == {"nodes": []}


def test_parse_json_strips_markdown_fence():
    text = "```json\n{\"nodes\": []}\n```"
    parsed, err = _parse_json_response(text)
    assert err is None
    assert parsed == {"nodes": []}


def test_parse_json_repairs_merged_node_objects():
    # Наблюдалось на практике (DeepSeek, temperature=0): модель забывает
    # `}, {` между элементами массива и склеивает N узлов в один JSON-объект
    # с повторяющимися ключами. Обычный dict(pairs) тихо оставил бы только
    # последний узел ("Биллинг") — 8 остальных исчезли бы без ошибки парсинга.
    merged = (
        '{"nodes": [{'
        '"name": "Аутентификация", "summary": "Вход.", "symbols": ["a.login"], '
        '"name": "Биллинг", "summary": "Счета.", "symbols": ["b.invoice"]'
        '}]}'
    )
    parsed, err = _parse_json_response(merged)
    assert err is None
    assert isinstance(parsed["nodes"][0], list)  # починка вернула список вместо одного dict

    warnings = []
    nodes = _validate_and_bucket(parsed, {"a.login", "b.invoice"}, warnings)
    by_name = {n["name"]: n for n in nodes}
    assert by_name["Аутентификация"]["symbols"] == ["a.login"]
    assert by_name["Биллинг"]["symbols"] == ["b.invoice"]
    assert any("склеенных" in w for w in warnings)


def test_parse_json_does_not_touch_normal_duplicate_free_objects():
    # Обычный ответ без повторов ключей проходит как раньше — dict как dict.
    parsed, err = _parse_json_response(
        '{"nodes": [{"name": "X", "summary": "s", "symbols": ["x.y"]}]}')
    assert err is None
    assert isinstance(parsed["nodes"][0], dict)
    assert parsed["nodes"][0]["name"] == "X"


def test_parse_json_unparseable_reports_error():
    parsed, err = _parse_json_response("это не json")
    assert parsed is None
    assert err is not None


def test_validate_and_bucket_drops_invalid_and_dedups():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    warnings = []
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, warnings)

    by_name = {n["name"]: n for n in nodes}
    assert "nonexistent.symbol" not in by_name["Биллинг"]["symbols"]
    # дублирующийся issue остаётся только в первом узле, где встретился
    assert "auth.tokens.issue" in by_name["Аутентификация"]["symbols"]
    assert "auth.tokens.issue" not in by_name["Общее"]["symbols"]
    assert any("nonexistent.symbol" in w for w in warnings)
    assert any("issue" in w and "более чем" in w for w in warnings)


def test_validate_and_bucket_unassigned_goes_to_unassigned_node():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, [])
    by_name = {n["name"]: n for n in nodes}
    assert set(by_name[UNASSIGNED_NODE]["symbols"]) == {
        "common.utils.normalize", "cycle.a.alpha", "cycle.b.beta",
    }


def test_assign_synthetic_modules_majority_vote():
    g = _graph()
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    nodes = _validate_and_bucket(FAKE_RESPONSE, real_ids, [])
    _assign_synthetic_modules(nodes, g["symbols"])
    m = node_map(nodes)

    assert m["auth.tokens.<module>"] == "Аутентификация"
    assert m["auth.login.<module>"] == "Аутентификация"
    assert m["billing.invoice.<module>"] == "Биллинг"
    # common.utils.<module>: hash_value+CACHE (Общее, 2) > normalize (Прочее, 1)
    assert m["common.utils.<module>"] == "Общее"
    # cycle.*.<module>: единственный реальный символ файла ушёл в Прочее
    assert m["cycle.a.<module>"] == UNASSIGNED_NODE
    assert m["cycle.b.<module>"] == UNASSIGNED_NODE


def test_group_repo_end_to_end_with_fake_provider():
    g = _graph()
    provider = FakeProvider()
    result = group_repo(g["symbols"], g["symbol_edges"], provider)

    assert result["unassigned_count"] == 3
    assert result["usage"]["input_tokens"] == 100
    assert len(provider.calls) == 1

    m = node_map(result["nodes"])
    # каждый символ (реальный и синтетический) получил ровно один узел
    all_ids = {s["id"] for s in g["symbols"]}
    assert set(m) == all_ids


def test_group_repo_unparseable_response_falls_back_to_all_unassigned():
    g = _graph()
    provider = FakeProvider(text="не json вообще")
    result = group_repo(g["symbols"], g["symbol_edges"], provider)

    assert any("распарсить" in w for w in result["warnings"])
    by_name = {n["name"]: n for n in result["nodes"]}
    real_ids = {s["id"] for s in g["symbols"] if not s.get("synthetic")}
    # реальные символы попадают в Прочее напрямую; счётчик качества
    # группировки считается ДО приписывания синтетических <module>...
    assert result["unassigned_count"] == len(real_ids)
    # ...но раз все реальные символы каждого файла тоже ушли в Прочее, по
    # majority vote туда же уходят и <module> — весь граф в одном узле.
    assert set(by_name[UNASSIGNED_NODE]["symbols"]) == {s["id"] for s in g["symbols"]}


def test_semantic_shared_ratio_via_node_map():
    # Общее хранит и hash_value, и CACHE, но их реальные вызыватели (issue,
    # signature) сидят в разных узлах (Аутентификация/Биллинг) -> shared.
    g = _graph()
    nodes = _validate_and_bucket(FAKE_RESPONSE,
                                 {s["id"] for s in g["symbols"] if not s.get("synthetic")}, [])
    _assign_synthetic_modules(nodes, g["symbols"])
    m = node_map(nodes)

    s = summarize_by_node(g["symbols"], g["symbol_edges"], m)
    assert s["shared_ratio"] > 0
    hash_value_owners = s["owners"]["common.utils.hash_value"]["owners"]
    assert set(hash_value_owners) == {"Аутентификация", "Биллинг"}


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
