"""Тесты парсинга .gitignore/.mindgraphignore (extract.py).

pathspec-семантика: паттерны каталогов, файловые glob, negation `!`.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph.extract import extract_repo


def _tmp(files: dict, gitignore: str = None, mindgraphignore: str = None):
    d = tempfile.mkdtemp(prefix="mindgraph_ignore_")
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    if gitignore is not None:
        with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write(gitignore)
    if mindgraphignore is not None:
        with open(os.path.join(d, ".mindgraphignore"), "w", encoding="utf-8") as fh:
            fh.write(mindgraphignore)
    return d


def _files(root):
    g = extract_repo(root)
    return sorted({s["file"] for s in g["symbols"]})


def test_gitignore_dir_exclusion():
    root = _tmp({"pkg/a.py": "x=1\n", "skipme/b.py": "x=1\n"},
                gitignore="skipme/\n")
    try:
        assert _files(root) == ["pkg/a.py"]
    finally:
        shutil.rmtree(root)


def test_gitignore_file_glob():
    root = _tmp({"pkg/a.py": "x=1\n", "pkg/gen.tmp.py": "x=1\n",
                 "pkg/b.py": "x=1\n"},
                gitignore="*.tmp.py\n")
    try:
        assert _files(root) == ["pkg/a.py", "pkg/b.py"]
    finally:
        shutil.rmtree(root)


def test_gitignore_negation_reincludes():
    root = _tmp({"skipme/keep.py": "x=1\n", "skipme/drop.py": "x=1\n"},
                gitignore="skipme/*\n!skipme/keep.py\n")
    try:
        assert _files(root) == ["skipme/keep.py"]
    finally:
        shutil.rmtree(root)


def test_mindgraphignore_extra_on_top_of_gitignore():
    root = _tmp({"pkg/a.py": "x=1\n", "docs/b.py": "x=1\n", "gen/c.py": "x=1\n"},
                gitignore="docs/\n", mindgraphignore="gen/\n")
    try:
        assert _files(root) == ["pkg/a.py"]
    finally:
        shutil.rmtree(root)


def test_builtin_ignore_dirs_still_apply():
    root = _tmp({"pkg/a.py": "x=1\n", ".venv/b.py": "x=1\n",
                 "__pycache__/c.py": "x=1\n"})
    try:
        assert _files(root) == ["pkg/a.py"]
    finally:
        shutil.rmtree(root)


def test_extra_ignore_argument():
    root = _tmp({"pkg/a.py": "x=1\n", "tests/b.py": "x=1\n"})
    try:
        assert _files(root) == ["pkg/a.py", "tests/b.py"]
        g = extract_repo(root, extra_ignore=("tests",))
        assert sorted({s["file"] for s in g["symbols"]}) == ["pkg/a.py"]
    finally:
        shutil.rmtree(root)


def test_repo_hash_changes_when_gitignore_edited():
    from mindgraph.emit import compute_repo_hash
    root = _tmp({"pkg/a.py": "x=1\n", "skipme/b.py": "x=1\n"},
                gitignore="skipme/\n")
    try:
        h1 = compute_repo_hash(root)
        with open(os.path.join(root, ".gitignore"), "a", encoding="utf-8") as fh:
            fh.write("pkg/\n")
        h2 = compute_repo_hash(root)
        assert h1 != h2
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
