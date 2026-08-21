"""Этап A — статическое извлечение символов и рёбер.

Только `ast` из стандартной библиотеки. Анализируемый код никогда не
импортируется и не исполняется: мы парсим исходники и не более того.

Виды символов (`kind`):

* ``function`` / ``method`` / ``class`` — обычные определения;
* ``binding``  — модульный синглтон: ``Name = <expr>`` на верхнем уровне
  (``db = SQLAlchemy()``, ``bp = Blueprint(...)``). Типовая shared-инфраструктура;
* ``module``   — псевдосимвол ``<module>`` на каждый файл, которому приписан весь
  код верхнего уровня (импорты, вызовы, регистрации вроде ``app = create_app()``).
  Помечен ``synthetic: true``; в рёбрах участвует как обычный символ.

Типы рёбер:

* ``call``     — символ вызывает символ (`ast.Call`);
* ``uses``     — символ упоминает символ в своём теле (любая ссылка по имени в
  контексте чтения: аргумент, аннотация, присваивание, база вызова);
* ``inherits`` — класс наследуется от внутрирепозиторного класса;
* ``contains`` — класс содержит метод.

Публичная точка входа — :func:`extract_repo`, возвращающая словарь с ключами
``symbols``, ``symbol_edges``, ``external`` и ``warnings``. Сборку полного
``graph.json`` (этап C / emit) делает другой модуль.
"""

from __future__ import annotations

import ast
import os
import sys

try:
    import pathspec
except ImportError:  # pathspec опционален на чтении — без него игноры только IGNORE_DIRS
    pathspec = None

# Каталоги, которые никогда не сканируем (см. §1 спеки). Разбор .gitignore
# добавится позже; здесь — жёстко зашитый минимум плюс *.egg-info.
# `migrations` — автогенерённые ревизии БД, не продуктовый код.
IGNORE_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__",
    ".mindgraph", ".mindvibe", "build", "dist", "migrations",
}

# Файлы, которые не считаем смысловым кодом (тест-раннер верхнего уровня).
IGNORE_FILES = {"tests.py"}


def _rel(root: str, path: str) -> str:
    """Путь относительно корня репы, всегда с прямыми слэшами."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _module_id(rel: str) -> str:
    """`auth/login.py` -> `auth.login`; пакетный `__init__.py` -> имя пакета."""
    parts = rel[:-3].split("/")  # срезаем '.py'
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _iter_python_files(root: str, extra_ignore=()):
    ignore = IGNORE_DIRS | set(extra_ignore)
    specs = _ignore_specs(root, extra_ignore)

    def _ignored(path_abs: str) -> bool:
        for base_dir, spec in specs:
            rel_to = os.path.relpath(path_abs, base_dir)
            if spec.match_file(rel_to):
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for d in sorted(dirnames):
            if d in ignore or d.endswith(".egg-info"):
                continue
            dpath = os.path.join(dirpath, d)
            if not _ignored(dpath):
                kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn in IGNORE_FILES:
                continue
            fpath = os.path.join(dirpath, fn)
            if not _ignored(fpath):
                yield fpath


# --- Игноры из .gitignore / .mindgraphignore --------------------------------

IGNORE_FILE_NAMES = (".gitignore", ".mindgraphignore")


def _iter_ignore_files(root: str, extra_ignore=()):
    """Yield ``(abs_path, rel_path)`` игнор-файлов (с обрезкой IGNORE_DIRS).

    Вложенные ``.gitignore`` действуют относительно своей директории; они
    тоже возвращаются. Каталоги из ``IGNORE_DIRS``/``extra_ignore`` не
    обходим вообще — их игноры не читаем.
    """
    ignore = IGNORE_DIRS | set(extra_ignore)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ignore and not d.endswith(".egg-info")
        )
        for name in IGNORE_FILE_NAMES:
            if name in filenames:
                p = os.path.join(dirpath, name)
                yield p, _rel(root, p)


def _ignore_specs(root: str, extra_ignore=()):
    """Список ``(base_dir, GitIgnoreSpec)`` по всем найденным игнор-файлам.

    Без pathspec — пустой список (игнорируются только IGNORE_DIRS).
    """
    if pathspec is None:
        return []
    specs = []
    for p, _relp in _iter_ignore_files(root, extra_ignore):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = [ln.rstrip("\n") for ln in fh]
        except OSError:
            continue
        specs.append((os.path.dirname(p), pathspec.GitIgnoreSpec.from_lines(lines)))
    return specs


def _signature(node: ast.AST) -> str:
    """Текст сигнатуры из ast.arguments + аннотаций (или `class Name(...)`)."""
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        return f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{node.name}({args}){ret}"


def _docstring(node: ast.AST):
    doc = ast.get_docstring(node)
    return doc[:200] if doc else None


# --- Символы ---------------------------------------------------------------

def _binding_names(targets):
    """Имена-цели верхнеуровневого присваивания (Name и Name из кортежей)."""
    names = []
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            names.extend(el.id for el in t.elts if isinstance(el, ast.Name))
    return names


def _binding_sig(name, value) -> str:
    return f"{name} = {ast.unparse(value)}"[:120]


def _collect_symbols(module_id: str, rel: str, tree: ast.Module):
    """Извлечь символы модуля: def/class/method, binding и псевдо-<module>.

    Каждая запись — dict с публичными полями графа плюс служебными (``_node`` —
    узел AST, ``_class`` — id класса для методов), которые вызывающий использует
    при резолве рёбер и потом отбрасывает.
    """
    symbols = []

    def _record(node, sym_id, kind, class_id, sig, doc, synthetic=False):
        symbols.append({
            "id": sym_id,
            "kind": kind,
            "file": rel,
            "lines": [node.lineno, node.end_lineno],
            "sig": sig,
            "doc": doc,
            "loc": node.end_lineno - node.lineno + 1,
            "synthetic": synthetic,
            "_node": node,
            "_class": class_id,
        })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _record(node, f"{module_id}.{node.name}", "function", None,
                    _signature(node), _docstring(node))
        elif isinstance(node, ast.ClassDef):
            class_id = f"{module_id}.{node.name}"
            _record(node, class_id, "class", None,
                    _signature(node), _docstring(node))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _record(item, f"{class_id}.{item.name}", "method", class_id,
                            _signature(item), _docstring(item))
        elif isinstance(node, ast.Assign):
            for name in _binding_names(node.targets):
                _record(node, f"{module_id}.{name}", "binding", None,
                        _binding_sig(name, node.value), None)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.target, ast.Name):
                _record(node, f"{module_id}.{node.target.id}", "binding", None,
                        _binding_sig(node.target.id, node.value), None)

    # Псевдосимвол <module>: держатель кода верхнего уровня (см. §модель).
    end = tree.body[-1].end_lineno if tree.body else 1
    symbols.append({
        "id": f"{module_id}.<module>",
        "kind": "module",
        "file": rel,
        "lines": [1, end],
        "sig": "<module>",
        "doc": None,
        "loc": end,
        "synthetic": True,
        "_node": tree,
        "_class": None,
    })
    return symbols


# --- Таблица импортов ------------------------------------------------------

def _import_table(module_id: str, tree: ast.Module, module_ids, symbol_ids,
                  external: set):
    """Построить `local_name -> ('symbol'|'module', fqid)` для одного файла.

    Резолвится только внутрирепозиторное; всё прочее уходит верхним пакетом в
    ``external`` и в таблицу не попадает.
    """
    table = {}
    package = module_id.split(".")[:-1]  # пакет, которому принадлежит файл

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                if full in module_ids:
                    table[alias.asname or full] = ("module", full)
                else:
                    external.add(full.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - (node.level - 1)]
                if node.module:
                    base_parts = base_parts + node.module.split(".")
                base = ".".join(base_parts)
            else:
                base = node.module or ""

            for alias in node.names:
                name, local = alias.name, alias.asname or alias.name
                submodule = f"{base}.{name}" if base else name
                symbol = f"{base}.{name}" if base else name
                if submodule in module_ids:
                    table[local] = ("module", submodule)
                elif base in module_ids:
                    table[local] = ("symbol", symbol)
                elif base:
                    external.add(base.split(".")[0])
                else:
                    external.add(name)
    return table


# --- Резолв ссылок ---------------------------------------------------------

def _resolve_ref(node, module_id, class_id, table, symbol_ids):
    """Вернуть id внутрирепозиторного символа, на который ссылается узел.

    Работает для `Name` и `Attribute` с базой-`Name`. None, если ссылка
    внешняя, локальная или не резолвится. Используется и для вызовов
    (`call.func`), и для упоминаний (`uses`), и для баз классов (`inherits`).
    """
    if isinstance(node, ast.Name):
        name = node.id
        if name in table:
            kind, fqid = table[name]
            return fqid if kind == "symbol" and fqid in symbol_ids else None
        cand = f"{module_id}.{name}"
        return cand if cand in symbol_ids else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        base, attr = node.value.id, node.attr
        if base == "self":
            cand = f"{class_id}.{attr}" if class_id else None
            return cand if cand and cand in symbol_ids else None
        if base in table:
            _, fqid = table[base]
            cand = f"{fqid}.{attr}"
            return cand if cand in symbol_ids else None
        cand = f"{module_id}.{base}.{attr}"
        return cand if cand in symbol_ids else None
    return None


def _body_scope(sym):
    """AST-узлы, которые «принадлежат» символу для поиска вызовов и упоминаний.

    Функция/метод — весь свой подграф (вложенные функции — часть родителя, §2).
    Класс и псевдо-<module> — только операторы своего уровня вне тел вложенных
    def/class (те принадлежат своим символам).
    """
    node = sym["_node"]
    if sym["kind"] in ("class", "module"):
        nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        return [st for st in node.body if not isinstance(st, nested)]
    return [node]


# --- Оркестрация -----------------------------------------------------------

def extract_repo(root: str, extra_ignore=()) -> dict:
    """Просканировать репу и вернуть символы, рёбра, external и warnings.

    ``extra_ignore`` — дополнительные имена каталогов, которые не сканируем
    (напр. ``tests``), поверх жёстко зашитого :data:`IGNORE_DIRS`.
    """
    root = os.path.abspath(root)
    warnings: list[str] = []
    parsed = []  # (module_id, rel, tree)

    for path in _iter_python_files(root, extra_ignore):
        rel = _rel(root, path)
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except SyntaxError as e:
            msg = f"{rel}:{e.lineno} syntax error, file skipped"
            warnings.append(msg)
            print(f"warning: {msg}", file=sys.stderr)
            continue
        except Exception as e:  # никогда не падаем из-за одного файла
            msg = f"{rel} could not be read ({e.__class__.__name__}), file skipped"
            warnings.append(msg)
            print(f"warning: {msg}", file=sys.stderr)
            continue
        parsed.append((_module_id(rel), rel, tree))

    module_ids = {mid for mid, _, _ in parsed}

    # Символы всех модулей + глобальное множество id.
    per_file = []  # (module_id, tree, [symbol dicts])
    all_symbols = []
    for mid, rel, tree in parsed:
        syms = _collect_symbols(mid, rel, tree)
        per_file.append((mid, tree, syms))
        all_symbols.extend(syms)
    symbol_ids = {s["id"] for s in all_symbols}

    external: set = set()
    raw_edges: dict = {}  # (from, to, type) -> weight

    def _add(src, dst, etype):
        if src == dst:  # само-рёбра выкидываем
            return
        key = (src, dst, etype)
        raw_edges[key] = raw_edges.get(key, 0) + 1

    for mid, tree, syms in per_file:
        table = _import_table(mid, tree, module_ids, symbol_ids, external)

        for sym in syms:
            if sym["kind"] == "binding":
                continue  # binding — только цель ссылок, его RHS в <module>

            node = sym["_node"]
            class_id = sym["_class"]

            if sym["kind"] == "class":
                # contains: класс -> его методы
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _add(sym["id"], f"{sym['id']}.{item.name}", "contains")
                # inherits: класс -> внутрирепозиторные базы
                for base in node.bases:
                    target = _resolve_ref(base, mid, None, table, symbol_ids)
                    if target is not None:
                        _add(sym["id"], target, "inherits")

            for scope in _body_scope(sym):
                for n in ast.walk(scope):
                    if isinstance(n, ast.Call):
                        target = _resolve_ref(n.func, mid, class_id, table,
                                              symbol_ids)
                        if target is not None:
                            _add(sym["id"], target, "call")
                    elif isinstance(n, (ast.Name, ast.Attribute)) \
                            and isinstance(n.ctx, ast.Load):
                        target = _resolve_ref(n, mid, class_id, table,
                                              symbol_ids)
                        if target is not None:
                            _add(sym["id"], target, "uses")

    # Дедуп call/uses по паре вершин: вызов — частный случай упоминания,
    # поэтому при наличии call между (A, B) избыточное uses для той же пары
    # выкидываем (call его поглощает). inherits/contains не трогаем.
    call_pairs = {(f, t) for (f, t, et) in raw_edges if et == "call"}
    for key in [k for k in raw_edges if k[2] == "uses" and (k[0], k[1]) in call_pairs]:
        del raw_edges[key]

    # Публичные символы без служебных полей, детерминированный порядок.
    symbols = sorted(
        ({k: v for k, v in s.items() if not k.startswith("_")}
         for s in all_symbols),
        key=lambda s: s["id"],
    )
    symbol_edges = sorted(
        ({"from": f, "to": t, "type": et, "weight": w}
         for (f, t, et), w in raw_edges.items()),
        key=lambda e: (e["from"], e["to"], e["type"]),
    )

    return {
        "symbols": symbols,
        "symbol_edges": symbol_edges,
        "external": sorted(external),
        "warnings": warnings,
    }
