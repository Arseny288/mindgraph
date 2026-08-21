"""Этап B2 — LLM-группировка узлов L1 в домены L0.

ОТДЕЛЬНЫЙ второй запрос к модели, полностью независимый от group.py:
group.py и назначение символов по узлам L1 (§3 спеки) остаются нетронутыми
— этот модуль их не импортирует для мутации и не трогает их файл.

На вход второму запросу идут ТОЛЬКО уже посчитанные узлы L1 (имя, summary,
размеры) и рёбра между ними — списки символов НЕ передаются вообще (модель
не должна ни видеть, ни тем более менять то, что уже решено на этапе B).
Ответ модели проставляет каждому узлу L1 поле ``parent`` (id домена) и
возвращает список доменов. :func:`assign_domains` — единственная точка
мутации: она никогда не трогает ``node["symbols"]``, а вызывающая сторона
(emit.py) проверяет это assert'ом до/после вызова (см. build_graph).

Если LLM недоступна (нет ключа/пакета) или ответ не парсится — тихий откат
на один домен "Проект" со всеми узлами внутри (тот же принцип "никогда не
падать целиком", что и в group.py/emit.py), с warning.
"""

from __future__ import annotations

import os
import re

# Чисто читающий импорт готовых констант/утилит group.py — сам файл group.py
# этим модулем не меняется и не переоткрывается.
from . import llmcache
from .group import (
    ANTHROPIC_MODEL, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, _parse_json_response,
)

UNASSIGNED_DOMAIN = "Проект"

_DOMAIN_SYSTEM_PROMPT = """Сгруппируй узлы графа кода в 4-6 доменов верхнего уровня — по смысловой
близости их назначения, не по алфавиту и не по размеру.

Имена доменов — короткие, на русском, в терминах предметной области
(например «Работа с данными», «Взаимодействие с пользователем»).

Каждый узел из списка обязан попасть ровно в один домен.

Ответь СТРОГО JSON, без markdown-заграждений (```), без преамбулы и без
пояснений до или после. Формат ответа:

{"domains": [{"name": "Работа с данными", "nodes": ["Хранилище данных", "Модели"]}]}
"""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", name.strip().lower()).strip("_")
    return s or "domain"


def _build_domain_user_prompt(nodes: list, node_edges: list) -> str:
    """Только имена/summary/размеры узлов L1 и рёбра между ними — без единого
    символа (см. docstring модуля)."""
    import json
    slim_nodes = [
        {"name": n["name"], "summary": n.get("summary", ""),
         "files": len(n["files"]), "symbols": len(n["symbols"]), "loc": n["loc"]}
        for n in nodes
    ]
    name_by_id = {n["id"]: n["name"] for n in nodes}
    slim_edges = [
        {"from": name_by_id[e["from"]], "to": name_by_id[e["to"]], "weight": e["weight"]}
        for e in node_edges if e["from"] in name_by_id and e["to"] in name_by_id
    ]
    payload = {"nodes": slim_nodes, "edges": slim_edges}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "raw": {}}


def _call_deepseek(system_prompt: str, user_prompt: str,
                   model: str | None = None) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY не задан (ни аргументом, ни в окружении/.env)")
    from openai import OpenAI  # ленивый импорт, как в group.py
    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=model or DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2000,  # список узлов L1 (5-15 штук) на порядок меньше списка символов
        temperature=0,
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
            "cache_write_tokens": 0,
            "raw": u.model_dump() if hasattr(u, "model_dump") else dict(u),
        })
    return {"text": text, "usage": usage}


def _call_anthropic(system_prompt: str, user_prompt: str,
                    model: str | None = None) -> dict:
    import anthropic  # ленивый импорт
    key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    resp = client.messages.create(
        model=model or ANTHROPIC_MODEL,
        max_tokens=2000,
        system=system_prompt,
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


def _call_provider(provider_name: str, system_prompt: str, user_prompt: str,
                   model: str | None = None) -> dict:
    if provider_name == "deepseek":
        return _call_deepseek(system_prompt, user_prompt, model)
    if provider_name == "anthropic":
        return _call_anthropic(system_prompt, user_prompt, model)
    raise ValueError(f"неизвестный провайдер {provider_name!r}")


def _validate_domains(parsed, nodes: list, warnings: list) -> list | None:
    """[{"name", "members": [node_name, ...]}] или None (откат на один домен)."""
    valid_names = {n["name"] for n in nodes}
    raw_domains = parsed.get("domains") if isinstance(parsed, dict) else None
    if not isinstance(raw_domains, list) or not raw_domains:
        warnings.append("ответ модели не содержит списка domains, откатываюсь на один домен")
        return None

    result = []
    assigned = set()
    for rd in raw_domains:
        if not isinstance(rd, dict) or "name" not in rd:
            warnings.append(f"пропущен некорректный домен в ответе модели: {rd!r}")
            continue
        members = []
        for name in rd.get("nodes", []):
            if name not in valid_names:
                warnings.append(f"домен {rd['name']!r}: узел {name!r} не существует, отброшен")
                continue
            if name in assigned:
                warnings.append(f"узел {name!r} назначен более чем в один домен, оставлен в первом")
                continue
            members.append(name)
            assigned.add(name)
        if members:
            result.append({"name": rd["name"], "members": members})

    unassigned = sorted(valid_names - assigned)
    if unassigned:
        result.append({"name": UNASSIGNED_DOMAIN, "members": unassigned})

    if not result:
        warnings.append("после валидации доменов не осталось ни одного, откатываюсь на один домен")
        return None
    return result


def _finalize(buckets: list, nodes: list) -> list:
    """Проставить node["parent"] и вернуть итоговый массив domains для graph.json.

    Единственное поле, которое мутируется на узлах, — ``parent``.
    ``node["symbols"]`` здесь никогда не читается и не пишется.
    """
    node_by_name = {n["name"]: n for n in nodes}
    out = []
    for b in buckets:
        did = "d_" + _slug(b["name"])
        out.append({"id": did, "name": b["name"]})
        for member_name in b["members"]:
            node_by_name[member_name]["parent"] = did
    return out


def assign_domains(nodes: list, node_edges: list, provider_name: str = "deepseek",
                   no_llm: bool = False, cache_base=None,
                   model: str | None = None) -> dict:
    """Оркестрация этапа B2. Мутирует ``nodes`` (добавляет ``parent``).

    ``cache_base`` — каталог кэша LLM-ответов; ``model`` — переопределение
    дефолтной модели провайдера (иначе DEEPSEEK_MODEL/ANTHROPIC_MODEL).

    Возвращает ``{"domains": [...], "warnings": [...], "usage": {...},
    "raw_text": str}``. При отсутствии LLM/провайдера или невалидном ответе —
    один домен :data:`UNASSIGNED_DOMAIN` на все узлы, без исключения (§1
    спеки: никогда не падать целиком).
    """
    warnings: list = []
    usage = _empty_usage()
    raw_text = ""
    buckets = None

    if not no_llm:
        system = _DOMAIN_SYSTEM_PROMPT
        user = _build_domain_user_prompt(nodes, node_edges)
        eff_model = model or (DEEPSEEK_MODEL if provider_name == "deepseek"
                              else ANTHROPIC_MODEL)
        cache_key = llmcache.prompt_key(provider_name, eff_model, system, user)
        resp = None
        if cache_base is not None:
            resp = llmcache.load(cache_base, cache_key)
        if resp is None:
            try:
                resp = _call_provider(provider_name, system, user, model)
            except Exception as e:  # нет ключа, нет пакета, сетевая ошибка и т.п.
                warnings.append(
                    f"LLM-провайдер {provider_name!r} недоступен для доменов ({e}); "
                    f"один домен {UNASSIGNED_DOMAIN!r} на все узлы")
                resp = None
            if resp is not None and cache_base is not None:
                llmcache.save(cache_base, cache_key, resp["text"], resp["usage"],
                              provider_name, eff_model)
        if resp is not None:
            raw_text = resp["text"]
            usage = resp["usage"]
            parsed, err = _parse_json_response(raw_text)
            if err:
                warnings.append(err)
            else:
                buckets = _validate_domains(parsed, nodes, warnings)

    if buckets is None:
        buckets = [{"name": UNASSIGNED_DOMAIN, "members": [n["name"] for n in nodes]}]

    domains = _finalize(buckets, nodes)
    return {"domains": domains, "warnings": warnings, "usage": usage, "raw_text": raw_text}
