"""serve.py — статик-сервер вьюера (§7 спеки): без бэкенда, без npm.

Отдаёт файлы из ``viewer/`` как есть; единственное исключение —
``/graph.json``, который подменяется на ``<repo_root>/.mindgraph/graph.json``
запрашиваемой репы. Вьюер про это не знает — просто делает
``fetch('/graph.json')`` относительно себя.
"""

from __future__ import annotations

import http.server
import importlib.resources
import os
import socketserver
import threading
import webbrowser


def viewer_dir() -> str:
    """Каталог статики вьюера из ПАКЕТА (см. pyproject package-data), а не
    относительно исходника. Работает и после честного ``pip install .`` (без
    ``-e``): файлы ``viewer/`` попадают в site-packages как package-data.

    ``importlib.resources.files("mindgraph")/"viewer"`` для обычной (не zip)
    установки — реальный путь на диске; ``as_file`` — страховка для
    zipimport, где ресурс живёт в архиве и нужна материализация во временный
    каталог.
    """
    traversable = importlib.resources.files("mindgraph") / "viewer"
    if hasattr(traversable, "__fspath__"):
        return os.fspath(traversable)
    # zipimport: ресурс в архиве — материализуем во временный каталог и
    # держим контекст открытым на всё время жизни сервера.
    return os.fspath(importlib.resources.as_file(traversable).__enter__())


def _make_handler(repo_root: str):
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")

    class Handler(http.server.SimpleHTTPRequestHandler):
        # .jsx не входит в mimetypes по умолчанию — без этого браузер
        # получает Content-Type: application/octet-stream и молча
        # отказывается выполнять <script type="module" src="*.jsx">.
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".jsx": "text/javascript"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=viewer_dir(), **kw)

        def translate_path(self, path):
            if path.split("?", 1)[0] == "/graph.json":
                return graph_file
            return super().translate_path(path)

        def log_message(self, fmt, *args):
            pass  # тихо — не спамить консоль на каждый статик-файл

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def make_server(repo_root: str, port: int = 5173):
    """Создать (но не запускать) HTTP-сервер вьюера для ``repo_root``.

    ``port=0`` даёт свободный порт ОС — порт можно взять из
    ``server.server_address[1]``. Отделено от :func:`run` ради тестов: сервер
    можно поднять в потоке и корректно погасить через ``shutdown()``.
    """
    repo_root = os.path.abspath(repo_root)
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")
    if not os.path.exists(graph_file):
        raise FileNotFoundError(
            f"{graph_file} не найден — сначала запусти mindgraph без --serve, "
            f"или вместе с ним (он строится перед подъёмом сервера)")
    return _ReusableTCPServer(("127.0.0.1", port), _make_handler(repo_root))


def run(repo_root: str, port: int = 5173, open_browser: bool = True) -> None:
    httpd = make_server(repo_root, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"вьюер: {url}  (Ctrl+C — остановить)")

    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        httpd.server_close()
