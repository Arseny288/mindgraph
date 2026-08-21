"""Этап C — вычисление shared-слоя.

Метрика отвечает на вопрос: **если узел A правит символ S, у кого это
сломается.** Ломается у того, кто зависит от S напрямую — поэтому ``owners``
считается по ПРЯМЫМ обратным рёбрам, без обхода в глубину:

    owners(S) = { node(P) | ребро P -> S типа call | uses | inherits }

Почему не транзитивное замыкание: достижимость без ограничения глубины меряет
связность графа («можно ли доползти от узла до символа по цепочке»), а в любой
связной программе ответ почти всегда «да». Это раздувает shared до меры
связности, а не изолируемости.

Что исключено из владения:

* ``contains`` (класс -> метод) — принадлежность метода классу не является
  зависимостью. Если метод реально зовут снаружи, для него и так есть прямое
  ``call``/``uses`` ребро. Само ребро ``contains`` в графе остаётся — оно нужно
  группировке на этапе 3.
* точки входа (``ENTRY_POINT_PATTERNS``) — shell-context / app-runner по
  определению трогают всё, поэтому владельцами не считаются (наравне с уже
  исключёнными из обхода ``tests.py`` и ``migrations/``).

Рядом сохранена транзитивная версия как :func:`compute_reach` — она отвечает на
другой полезный вопрос («что узел вообще способен задеть»); из неё берётся
``reach_ratio`` для сравнения.

В no-LLM режиме роль «узла» играет пакет (каталог файла символа), ``proxy=package``.
В этапе 3 это заменится узлами LLM-группировки.

Известное ограничение ``proxy=package``: для репы с одним плоским пакетом (все
файлы в одном каталоге, без подпакетов — например ``psf/requests``, где
``models.py``/``sessions.py``/``utils.py`` лежат рядом) вся библиотека
схлопывается в один узел, и межфайловое использование становится
внутриузловым — ``shared_ratio`` занижается почти до нуля, хотя реального
шаринга (через ``models``/``sessions``/``utils``) много. Это не потеря рёбер,
а огрубление единицы сравнения: под ``proxy=module`` та же репа даёт разумную
картину (см. прогон на requests: 0.2% -> 5.4%, топ shared — как раз
``models.PreparedRequest``/``models.Response``/``structures.CaseInsensitiveDict``).
Для сравнения ``shared_ratio`` между разными репами по ``package`` учитывай эту
асимметрию отдельно; окончательно закроет её узел LLM-группировки в этапе 3.
"""

from __future__ import annotations

import fnmatch

# Рёбра, по которым считается владение (contains намеренно исключён).
OWNER_EDGE_TYPES = frozenset({"call", "uses", "inherits"})

# Точки входа: их символы не считаются владельцами. Список паттернов (glob по
# пути или по имени файла), а не один захардкоженный файл.
ENTRY_POINT_PATTERNS = ("wsgi.py", "asgi.py", "manage.py", "run.py", "app.py",
                        "__main__.py")


def node_of(file: str, proxy: str = "package") -> str:
    """Узел символа под выбранный структурный прокси.

    ``package`` (по умолчанию) — каталог файла (`app/api/x.py` -> `app.api`);
    ``module``  — сам файл как модуль (`app/api/x.py` -> `app.api.x`).
    """
    if proxy == "module":
        mod = file[:-3] if file.endswith(".py") else file
        parts = mod.split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else "<root>"
    directory = file.rsplit("/", 1)[0] if "/" in file else ""
    return directory.replace("/", ".") if directory else "<root>"


def _is_entry_point(file: str, patterns) -> bool:
    base = file.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(file, p) or fnmatch.fnmatch(base, p)
               for p in patterns)


def compute_owners(symbols, edges, entry_points=ENTRY_POINT_PATTERNS,
                   proxy="package") -> dict:
    """Прямые владельцы: {symbol_id: {'node', 'owners': [...], 'shared': bool}}.

    owners(S) — узлы прямых предшественников S по рёбрам call/uses/inherits,
    исключая предшественников из точек входа. Узел — структурный proxy
    (package/module); см. :func:`compute_owners_by_node` для произвольного
    отображения символ -> узел (этап 3, LLM-группировка).
    """
    node = {s["id"]: node_of(s["file"], proxy) for s in symbols}
    entry = {s["id"] for s in symbols
             if _is_entry_point(s["file"], entry_points)}
    return compute_owners_by_node(symbols, edges, node, entry_ids=entry)


def compute_owners_by_node(symbols, edges, node_of_symbol, entry_ids=frozenset()) -> dict:
    """Как :func:`compute_owners`, но узел задаётся явным отображением
    ``{symbol_id: node_name}`` вместо структурного proxy, а точки входа —
    уже готовым множеством id (а не glob-паттернами файлов).

    Используется для смысловых узлов из LLM-группировки (этап 3) — там узел
    не выводится из пути файла, а приходит от модели.
    """
    owners: dict = {sid: set() for sid in node_of_symbol}
    for e in edges:
        if e["type"] not in OWNER_EDGE_TYPES:
            continue
        src, dst = e["from"], e["to"]
        if dst not in owners or src not in node_of_symbol or src in entry_ids:
            continue
        owners[dst].add(node_of_symbol[src])

    return {
        sid: {"node": node_of_symbol[sid], "owners": sorted(ns), "shared": len(ns) > 1}
        for sid, ns in owners.items()
    }


def compute_reach(symbols, edges, proxy="package") -> dict:
    """Транзитивная версия (старая): обратный обход по ВСЕМ рёбрам, с visited.

    Отвечает на вопрос «что узел вообще способен задеть». Собственный узел
    символа всегда входит (символ достижим из себя).
    """
    node = {s["id"]: node_of(s["file"], proxy) for s in symbols}
    ancestors: dict = {}
    for e in edges:
        ancestors.setdefault(e["to"], set()).add(e["from"])

    result = {}
    for sid in node:
        seen = set()
        stack = [sid]
        while stack:  # visited гасит циклы
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(ancestors.get(cur, ()))
        owners = sorted({node[x] for x in seen if x in node})
        result[sid] = {"owners": owners, "shared": len(owners) > 1}
    return result


def summarize(symbols, edges, entry_points=ENTRY_POINT_PATTERNS,
              proxy="package") -> dict:
    """Сводка: shared_ratio (прямой) и reach_ratio (транзитивный) рядом.

    Метрика считается по РЕАЛЬНЫМ символам (function/method/class/binding).
    Синтетические псевдо-<module> в знаменатель не входят — они не единицы кода,
    а корни/держатели верхнего уровня; в рёбрах как источники владения участвуют.
    """
    direct = compute_owners(symbols, edges, entry_points, proxy)
    reach = compute_reach(symbols, edges, proxy)

    measured = [s["id"] for s in symbols if not s.get("synthetic")]
    total = len(measured)

    shared = {sid: direct[sid] for sid in measured if direct[sid]["shared"]}
    reach_shared = sum(1 for sid in measured if reach[sid]["shared"])

    hist: dict = {}
    for sid in measured:
        n = len(direct[sid]["owners"])
        hist[n] = hist.get(n, 0) + 1

    ranked = sorted(shared.items(),
                    key=lambda kv: (-len(kv[1]["owners"]), kv[0]))

    return {
        "symbols": total,
        "synthetic": len(symbols) - total,
        "proxy": proxy,
        "nodes": sorted({r["node"] for r in direct.values()}),
        "shared_count": len(shared),
        "shared_ratio": (len(shared) / total) if total else 0.0,
        "reach_shared_count": reach_shared,
        "reach_ratio": (reach_shared / total) if total else 0.0,
        "owners_hist": dict(sorted(hist.items())),
        "top_shared": [(sid, r["owners"]) for sid, r in ranked],
        "owners": direct,
    }


def summarize_by_node(symbols, edges, node_of_symbol, entry_ids=frozenset()) -> dict:
    """Как :func:`summarize`, но на явном отображении символ -> узел (этап 3).

    Без ``reach_ratio`` — транзитивный обзор имеет смысл только для
    структурного proxy (§ модуль-докстринг); на смысловых узлах достаточно
    прямого ``shared_ratio`` для сравнения с package/module.
    """
    direct = compute_owners_by_node(symbols, edges, node_of_symbol, entry_ids)

    measured = [s["id"] for s in symbols if not s.get("synthetic")]
    total = len(measured)

    shared = {sid: direct[sid] for sid in measured if direct[sid]["shared"]}

    hist: dict = {}
    for sid in measured:
        n = len(direct[sid]["owners"])
        hist[n] = hist.get(n, 0) + 1

    ranked = sorted(shared.items(),
                    key=lambda kv: (-len(kv[1]["owners"]), kv[0]))

    return {
        "symbols": total,
        "synthetic": len(symbols) - total,
        "nodes": sorted({r["node"] for r in direct.values()}),
        "shared_count": len(shared),
        "shared_ratio": (len(shared) / total) if total else 0.0,
        "owners_hist": dict(sorted(hist.items())),
        "top_shared": [(sid, r["owners"]) for sid, r in ranked],
        "owners": direct,
    }
