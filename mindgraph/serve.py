"""serve.py — статик-сервер вьюера (§7 спеки): без бэкенда, без npm.

Отдаёт файлы из ``viewer/`` как есть; единственное исключение —
``/graph.json``, который подменяется на ``<repo_root>/.mindgraph/graph.json``
запрашиваемой репы. Вьюер про это не знает — просто делает
``fetch('/graph.json')`` относительно себя.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import webbrowser

VIEWER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer")


def _make_handler(repo_root: str):
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")

    class Handler(http.server.SimpleHTTPRequestHandler):
        # .jsx не входит в mimetypes по умолчанию — без этого браузер
        # получает Content-Type: application/octet-stream и молча
        # отказывается выполнять <script type="module" src="*.jsx">.
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".jsx": "text/javascript"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=VIEWER_DIR, **kw)

        def translate_path(self, path):
            if path.split("?", 1)[0] == "/graph.json":
                return graph_file
            return super().translate_path(path)

        def log_message(self, fmt, *args):
            pass  # тихо — не спамить консоль на каждый статик-файл

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def run(repo_root: str, port: int = 5173, open_browser: bool = True) -> None:
    repo_root = os.path.abspath(repo_root)
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")
    if not os.path.exists(graph_file):
        raise FileNotFoundError(
            f"{graph_file} не найден — сначала запусти mindgraph без --serve, "
            f"или вместе с ним (он строится перед подъёмом сервера)")

    httpd = _ReusableTCPServer(("127.0.0.1", port), _make_handler(repo_root))
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
