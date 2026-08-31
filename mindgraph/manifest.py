"""manifest.py — манифест стабильных узлов ``.mindvibe/nodes.yaml`` (фаза 1.5).

Манифест — это git-версионируемый «костяк» смысловых узлов. Он фиксирует
идентичность узла (id, title, пути владения), чтобы LLM при пересчёте НЕ
переименовывал и не переразбивал существующие узлы: символы, попавшие под
``own_paths`` узла, приписываются к нему механически, а модель классифицирует
только символы, не приписанные ни к одному узлу.

Формат:

.. code-block:: yaml

    nodes:
      - id: auth
        title: Аутентификация
        own_paths: ["app/auth/**", "app/tokens.py"]

``own_paths`` — gitignore-глобы по пути файла относительно корня репы.
"""

from __future__ import annotations

import os

import yaml

try:
    import pathspec
except ImportError:
    pathspec = None

MANIFEST_REL = os.path.join(".mindvibe", "nodes.yaml")


def mindvibe_root(root: str) -> str:
    """Корень ГЛАВНОГО репозитория, где лежит ``.mindvibe/`` (не worktree).

    Сессии идут в ``git worktree``: у worktree ``.git`` — это ФАЙЛ
    ``gitdir: <main>/.git/worktrees/<name>``. Если резолвить ``.mindvibe`` от
    рабочей директории, каждая сессия стартует с пустым манифестом и памятью.
    Поэтому поднимаемся к главному репо по этому указателю, если ``.mindvibe``
    в текущем корне отсутствует.
    """
    root = os.path.abspath(root)

    def _has(d):
        return os.path.isdir(os.path.join(d, ".mindvibe"))

    if _has(root):
        return root

    gitfile = os.path.join(root, ".git")
    if os.path.isfile(gitfile):
        try:
            content = open(gitfile, encoding="utf-8").read()
            if content.startswith("gitdir:"):
                gitdir = content.split(":", 1)[1].strip()
                # <main>/.git/worktrees/<name> -> <main> (три уровня вверх)
                main_root = os.path.abspath(os.path.join(
                    gitdir, os.pardir, os.pardir, os.pardir))
                if _has(main_root):
                    return main_root
        except OSError:
            pass
    return root


def manifest_path(root: str) -> str:
    return os.path.join(mindvibe_root(root), MANIFEST_REL)


def notes_dir(root: str) -> str:
    return os.path.join(mindvibe_root(root), ".mindvibe", "notes")


def read_note(root: str, node_id: str) -> str | None:
    """Память узла ``.mindvibe/notes/<node_id>.md``; нет файла -> None.

    Грузится ТОЛЬКО при открытом узле (агент вызывает при ``open_node``).
    Проектного файла уровня не существует.
    """
    path = os.path.join(notes_dir(root), f"{node_id}.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def write_note(root: str, node_id: str, content: str) -> str:
    path = os.path.join(notes_dir(root), f"{node_id}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def load(root: str) -> list:
    """Прочитать манифест; битый/отсутствующий -> пустой список (не фатально)."""
    p = manifest_path(root)
    if not os.path.exists(p):
        return []
    try:
        data = yaml.safe_load(open(p, encoding="utf-8").read())
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    nodes = []
    for n in data.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") is not None and n.get("title") is not None:
            nodes.append({
                "id": str(n["id"]),
                "title": str(n["title"]),
                "own_paths": [str(x) for x in (n.get("own_paths") or [])],
            })
    return nodes


def _spec(own_paths: list):
    if pathspec is None:
        return None
    return pathspec.PathSpec.from_lines("gitignore", list(own_paths))


def match_any(manifest_nodes: list, file: str) -> dict | None:
    """Первый узел манифеста, чей ``own_paths`` покрывает файл (или None)."""
    for n in manifest_nodes:
        spec = _spec(n["own_paths"])
        if spec is not None and spec.match_file(file):
            return n
    return None


def preassign(real_symbols: list, manifest_nodes: list) -> dict:
    """``{symbol_id: node_id}`` для символов, покрытых манифестом.

    Символ приписывается узлу по ФАЙЛУ (own_paths) и по СТАБИЛЬНОМУ id; модель
    его не увидит.
    """
    pinned: dict = {}
    by_file: dict = {}
    for s in real_symbols:
        by_file.setdefault(s["file"], []).append(s["id"])
    for n in manifest_nodes:
        spec = _spec(n["own_paths"])
        if spec is None:
            continue
        for f, sids in by_file.items():
            if spec.match_file(f):
                for sid in sids:
                    pinned.setdefault(sid, n["id"])
    return pinned
