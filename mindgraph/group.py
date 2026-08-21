"""Этап B — LLM-группировка символов в смысловые узлы.

Провайдер абстрагирован за :class:`LLMProvider` с одним методом
``group(symbols, edges) -> dict``. Обе реализации (:class:`DeepSeekProvider`,
:class:`AnthropicProvider`) используют ОДИН И ТОТ ЖЕ промпт — `SYSTEM_PROMPT`
и :func:`_build_user_prompt` — чтобы сравнение провайдеров было честным:
разница только в клиенте API, не в тексте запроса. Парсинг/валидация ответа
(§3 спеки) тоже общие, вне провайдера — см. :func:`group_repo`.

Порядок промпта: сначала неизменная часть (системная инструкция, список
символов и рёбер), volatile-части сейчас нет — весь граф статичен на один
прогон. Это даёт DeepSeek стабильный кэшируемый префикс "из коробки".

Батчинг не реализован: контекст обоих провайдеров (DeepSeek v4 — 1M,
Anthropic — 1M) вмещает любую нашу репу одним запросом (см. §3 спеки).

**Тела функций в промпт никогда не уходят** — только `id`, `sig`, `doc` и
рёбра между id.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod

from . import llmcache

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
# deepseek-chat / deepseek-reasoner отключены с 2026-07-24 — не использовать.

ANTHROPIC_MODEL = "claude-opus-5"  # см. заметку в docstring AnthropicProvider

UNASSIGNED_NODE = "Прочее"

_SYSTEM_PROMPT_TEMPLATE = """Сгруппируй символы Python-репозитория в {count_spec} смысловых узлов по НАЗНАЧЕНИЮ,
а не по расположению в файлах. Один узел обычно размазан по нескольким
файлам; один файл может принадлежать нескольким узлам.

Имена узлов — короткие, на русском, в терминах предметной области
(например «Аутентификация», «Хранилище», «Уведомления»), НЕ в терминах
реализации (не «Модуль auth», не «app.api»).

Каждый символ из списка обязан попасть ровно в один узел.

Ответь СТРОГО JSON, без markdown-заграждений (```), без преамбулы и без
пояснений до или после. Формат ответа:

{{"nodes": [{{"name": "Аутентификация",
            "summary": "одно предложение о назначении узла",
            "symbols": ["auth.login.verify_token", "auth.tokens.issue"]}}]}}
"""


def _system_prompt(node_count: int | None = None) -> str:
    """Промпт с фиксированным диапазоном узлов (дефолт) или точным K.

    ``node_count`` используется только для контрольных прогонов при
    сравнении с фиксированной гранулярностью структурного proxy (см.
    ``run_confound.py`` — K=6 против package-proxy на 6 узлах). В остальном
    промпт идентичен между вызовами — это и есть честное сравнение.
    """
    count_spec = "5-15" if node_count is None else f"ровно {node_count}"
    return _SYSTEM_PROMPT_TEMPLATE.format(count_spec=count_spec)


# Обратная совместимость: старое имя, дефолтный (5-15) промпт.
SYSTEM_PROMPT = _system_prompt()


def _build_user_prompt(symbols, edges) -> str:
    """Единый user-промпт для обоих провайдеров.

    Синтетические псевдо-<module> не передаются как единицы для группировки
    (это не единицы смысла — см. shared.py), но рёбра, где они участвуют
    (например ``app.<module> -> app.create_app``), остаются в списке рёбер:
    они показывают модели, что top-level код реально трогает эти символы.
    """
    real_ids = {s["id"] for s in symbols if not s.get("synthetic")}
    slim_symbols = [
        {"id": s["id"], "sig": s["sig"], "doc": s["doc"]}
        for s in symbols if s["id"] in real_ids
    ]
    slim_edges = [{"from": e["from"], "to": e["to"], "type": e["type"]}
                  for e in edges]
    payload = {"symbols": slim_symbols, "edges": slim_edges}
    # sort_keys — детерминированный байт-в-байт префикс между прогонами одной
    # и той же репы, важно для кэша по совпадению префикса у DeepSeek.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}


# --- Провайдеры --------------------------------------------------------

class LLMProvider(ABC):
    """Один метод: сходить в модель и вернуть сырой текст ответа + usage.

    Парсинг JSON, срез markdown-заграждений, валидация id и сборка узлов —
    общие для всех провайдеров, см. :func:`group_repo`. Провайдер отвечает
    только за сетевой вызов и нормализацию usage под общий формат:
    ``{"input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "raw"}``.
    """

    @abstractmethod
    def group(self, symbols, edges, node_count: int | None = None) -> dict:
        """Вернуть ``{"text": <сырой ответ модели>, "usage": {...}}``.

        ``node_count`` — точное число узлов вместо дефолтного диапазона
        5-15 (контрольные прогоны на фиксированной гранулярности)."""


class DeepSeekProvider(LLMProvider):
    """OpenAI-совместимый клиент, эндпоинт DeepSeek.

    Модель — ``deepseek-v4-flash`` (1M контекст). ``deepseek-chat`` и
    ``deepseek-reasoner`` — отключённые алиасы, не использовать.

    thinking выключен явно (``{"type": "disabled"}``): он включён по
    умолчанию и биллится как выход, а для задачи классификации не нужен.

    ``temperature=0`` — детерминированная (в пределах того, что вообще может
    дать API-провайдер) раскладка: инструмент должен давать одну и ту же
    группировку между прогонами на одной и той же репе, иначе граница узла
    "мигает" от запуска к запуску без единой причины.
    """

    name = "deepseek"

    def __init__(self, api_key: str | None = None, model: str = DEEPSEEK_MODEL,
                 base_url: str = DEEPSEEK_BASE_URL, max_tokens: int = 8000,
                 temperature: float = 0):
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY не задан (ни аргументом, ни в окружении/.env)")
        from openai import OpenAI  # ленивый импорт — не тянуть зависимость без нужды
        self._client = OpenAI(api_key=key, base_url=base_url)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def group(self, symbols, edges, node_count: int | None = None) -> dict:
        user_prompt = _build_user_prompt(symbols, edges)
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _system_prompt(node_count)},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=self.max_tokens,
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
                "cache_write_tokens": 0,  # DeepSeek не тарифицирует запись кэша отдельно
                "raw": u.model_dump() if hasattr(u, "model_dump") else dict(u),
            })
        return {"text": text, "usage": usage}


class AnthropicProvider(LLMProvider):
    """Официальный Anthropic SDK.

    Модель по умолчанию — ``claude-opus-5``. Для этой задачи (короткая
    классификация ~100-1500 записей в JSON) ставим ``output_config.effort:
    "low"`` — экономит токены на простой задаче без потери качества
    (см. рекомендации по effort в référence). Thinking НЕ отключаем явно:
    отключение thinking на Opus 5 может протащить вызов инструмента или
    служебные теги в видимый текст — тут инструментов нет, но эффект тот же
    для обычного текста; вместо этого используем низкий effort, что и так
    держит thinking (если он вообще включится) коротким.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str = ANTHROPIC_MODEL,
                 max_tokens: int = 8000):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        import anthropic  # ленивый импорт
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def group(self, symbols, edges, node_count: int | None = None) -> dict:
        user_prompt = _build_user_prompt(symbols, edges)
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_system_prompt(node_count),
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


PROVIDERS = {"deepseek": DeepSeekProvider, "anthropic": AnthropicProvider}


def make_provider(name: str = "deepseek", **kwargs) -> LLMProvider:
    if name not in PROVIDERS:
        raise ValueError(f"неизвестный провайдер {name!r}, доступны: {sorted(PROVIDERS)}")
    return PROVIDERS[name](**kwargs)


# --- Парсинг и валидация ответа (общие для всех провайдеров) -----------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _dedupe_or_split_pairs(pairs):
    """``object_pairs_hook`` для json.loads: чинит наблюдавшийся на практике
    сбой генерации — модель забывает ``}, {`` между элементами массива узлов
    и склеивает несколько JSON-объектов в один с повторяющимися ключами.

    Обычный ``dict(pairs)`` в этом случае молча оставляет последнее значение
    каждого повторяющегося ключа — из 12 узлов остаётся один, остальные 11
    исчезают без единой ошибки парсинга (JSON валиден, просто не то, что
    написали). Если ключи объекта — РОВНО периодический повтор одного и того
    же набора (k1..kn, k1..kn, ...), считаем это N склеенными объектами и
    возвращаем список словарей вместо одного словаря. Иначе — обычное
    поведение dict (последнее значение побеждает, как и раньше).
    """
    keys = [k for k, _ in pairs]
    n = len(keys)
    period = len(dict.fromkeys(keys))  # число уникальных ключей, в порядке появления
    if n < 2 or period == n or period == 0 or n % period != 0:
        return dict(pairs)
    cycle = keys[:period]
    if keys != cycle * (n // period):
        return dict(pairs)  # не чистый период — не наш паттерн, не рискуем
    return [dict(pairs[i:i + period]) for i in range(0, n, period)]


def _parse_json_response(text: str):
    """json.loads (с починкой склеенных объектов), а при неудаче — срезать
    markdown-заграждения и повторить."""
    try:
        return json.loads(text, object_pairs_hook=_dedupe_or_split_pairs), None
    except json.JSONDecodeError:
        pass
    stripped = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(stripped, object_pairs_hook=_dedupe_or_split_pairs), None
    except json.JSONDecodeError as e:
        return None, f"не удалось распарсить ответ модели как JSON: {e}"


def _validate_and_bucket(parsed, real_ids: set, warnings: list) -> list:
    """Провалидировать узлы модели, собрать неназначенные в UNASSIGNED_NODE.

    Возвращает список узлов вида {"name", "summary", "symbols"}.
    """
    nodes = []
    assigned = set()

    raw_nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(raw_nodes, list):
        warnings.append("ответ модели не содержит списка nodes, group пуст")
        raw_nodes = []

    # _dedupe_or_split_pairs мог вернуть list вместо dict для элемента-склейки
    # (несколько узлов, слипшихся в один объект) — разворачиваем на один уровень.
    flat_nodes = []
    for rn in raw_nodes:
        if isinstance(rn, list):
            warnings.append(f"починены {len(rn)} склеенных в один JSON-объект узла модели")
            flat_nodes.extend(rn)
        else:
            flat_nodes.append(rn)
    raw_nodes = flat_nodes

    for rn in raw_nodes:
        if not isinstance(rn, dict) or "name" not in rn:
            warnings.append(f"пропущен некорректный узел в ответе модели: {rn!r}")
            continue
        ids = []
        for sid in rn.get("symbols", []):
            if sid not in real_ids:
                warnings.append(f"узел {rn['name']!r}: символ {sid!r} не существует, отброшен")
                continue
            if sid in assigned:
                warnings.append(f"символ {sid!r} назначен более чем в один узел, "
                                f"оставлен в первом ({', '.join(n['name'] for n in nodes if sid in n['symbols']) or '?'})")
                continue
            ids.append(sid)
            assigned.add(sid)
        nodes.append({"name": rn["name"], "summary": rn.get("summary", ""), "symbols": ids})

    unassigned = sorted(real_ids - assigned)
    if unassigned:
        nodes.append({"name": UNASSIGNED_NODE,
                     "summary": "Символы, не попавшие ни в один смысловой узел.",
                     "symbols": unassigned})
    return nodes


def _assign_synthetic_modules(nodes: list, symbols: list) -> None:
    """Приписать псевдо-<module> узлу большинства реальных символов их файла.

    LLM группирует только реальные символы (см. _build_user_prompt); чтобы
    top-level код (импорты, `app = create_app()`) тоже участвовал в подсчёте
    владения на смысловых узлах, каждому <module> нужен узел. Берём узел,
    которому принадлежит больше всего реальных символов того же файла;
    если файл вообще не дал реальных символов узлам (маловероятно — тогда
    и <module> там пуст) — узел UNASSIGNED_NODE.

    Мутирует `nodes`: дописывает id синтетических символов в найденный узел.
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
    """{"symbol_id": "node_name"} из результата группировки — для shared.py."""
    return {sid: n["name"] for n in nodes for sid in n["symbols"]}


def group_repo(symbols, edges, provider: LLMProvider, node_count: int | None = None,
               cache_base=None) -> dict:
    """Оркестрация этапа B: один вызов провайдера + общий парсинг/валидация.

    ``node_count`` — прокинуть точное K вместо дефолтного диапазона 5-15
    (контрольные прогоны на фиксированной гранулярности).

    ``cache_base`` — каталог кэша LLM-ответов (см. llmcache.py). Если задан и
    провайдер несёт атрибуты ``name``/``model``, ответ кэшируется по хэшу
    промпта и повторный вызов с тем же графом не ходит в API.

    Возвращает ``{"nodes": [...], "unassigned_count", "warnings", "usage",
    "raw_text"}``. ``nodes`` уже включает синтетические <module> (см.
    :func:`_assign_synthetic_modules`) — готов для :func:`node_map`.
    ``raw_text`` — сырой ответ модели, для сохранения/отладки контрольных
    прогонов, которые не должны требовать повторного платного вызова.
    """
    real_ids = {s["id"] for s in symbols if not s.get("synthetic")}

    system = _system_prompt(node_count)
    user = _build_user_prompt(symbols, edges)
    cache_key = None
    result = None

    if cache_base is not None and hasattr(provider, "name") \
            and hasattr(provider, "model"):
        cache_key = llmcache.prompt_key(provider.name, provider.model, system, user)
        result = llmcache.load(cache_base, cache_key)

    if result is None:
        result = provider.group(symbols, edges, node_count=node_count)
        if cache_key is not None:
            llmcache.save(cache_base, cache_key, result["text"], result["usage"],
                          provider.name, provider.model)

    warnings: list = []
    parsed, err = _parse_json_response(result["text"])
    if err:
        warnings.append(err)
        parsed = {"nodes": []}

    nodes = _validate_and_bucket(parsed, real_ids, warnings)
    # Индикатор качества группировки — считаем ДО приписывания синтетических
    # <module>: те попадают в UNASSIGNED_NODE через majority vote (см.
    # _assign_synthetic_modules), а не потому что модель их не разгруппировала
    # (их ей никогда и не показывали — см. _build_user_prompt).
    unassigned_count = len(next(
        (n["symbols"] for n in nodes if n["name"] == UNASSIGNED_NODE), []))

    _assign_synthetic_modules(nodes, symbols)

    return {
        "nodes": nodes,
        "unassigned_count": unassigned_count,
        "warnings": warnings,
        "usage": result["usage"],
        "raw_text": result["text"],
    }
