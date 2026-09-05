"""serve.py — the viewer's static server (§7 of the spec): no backend, no npm.

Serves files from ``viewer/`` as-is; the only exception is ``/graph.json``, which
is replaced with ``<repo_root>/.mindgraph/graph.json`` of the requested repo. The
viewer does not know about this — it just does ``fetch('/graph.json')`` relative
to itself.
"""

from __future__ import annotations

import http.server
import importlib.resources
import os
import socketserver
import threading
import webbrowser


def viewer_dir() -> str:
    """The viewer's static directory from the PACKAGE (see pyproject package-data),
    not relative to the source file. Works even after a real ``pip install .``
    (without ``-e``): the ``viewer/`` files land in site-packages as package-data.

    ``importlib.resources.files("mindgraph")/"viewer"`` for a normal (non-zip)
    install — a real path on disk; ``as_file`` — insurance for zipimport, where
    the resource lives in an archive and needs materialization into a temporary
    directory.
    """
    traversable = importlib.resources.files("mindgraph") / "viewer"
    if hasattr(traversable, "__fspath__"):
        return os.fspath(traversable)
    # zipimport: the resource is in an archive — materialize it into a temporary
    # directory and keep the context open for the server's whole lifetime.
    return os.fspath(importlib.resources.as_file(traversable).__enter__())


def spa_fallback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    """SPA fallback for a client-side route (deep links surviving reload/direct
    paste — see main.jsx's history.pushState navigation): if the request's
    path doesn't resolve to a REAL file on disk and doesn't look like an
    asset request (no extension on its last segment — .js/.css/.json/fonts/
    images all keep their normal, genuine-404-on-missing behaviour), serve
    index.html with 200 instead of a bare 404. Returns True if it served the
    fallback (caller should stop and not also call the normal handler).
    """
    path = handler.path.split("?", 1)[0]
    fs_path = handler.translate_path(path)
    if os.path.isfile(fs_path):
        return False  # a real file — let the normal handler serve it
    ext = os.path.splitext(os.path.basename(path.rstrip("/")))[1]
    if ext:
        return False  # looks like an asset request — a real 404 is correct
    index_path = os.path.join(handler.directory, "index.html")
    try:
        with open(index_path, "rb") as f:
            body = f.read()
    except OSError:
        return False
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _make_handler(repo_root: str):
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")

    class Handler(http.server.SimpleHTTPRequestHandler):
        # .jsx is not in the default mimetypes — without this the browser gets
        # Content-Type: application/octet-stream and silently refuses to execute
        # <script type="module" src="*.jsx">.
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".jsx": "text/javascript"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=viewer_dir(), **kw)

        def translate_path(self, path):
            if path.split("?", 1)[0] == "/graph.json":
                return graph_file
            return super().translate_path(path)

        def do_GET(self):
            if spa_fallback(self):
                return
            super().do_GET()

        def log_message(self, fmt, *args):
            pass  # quiet — don't spam the console for every static file

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def make_server(repo_root: str, port: int = 5173):
    """Create (but do not start) the viewer's HTTP server for ``repo_root``.

    ``port=0`` gives a free OS port — the port can be read from
    ``server.server_address[1]``. Separated from :func:`run` for tests: the server
    can be started in a thread and cleanly stopped via ``shutdown()``.
    """
    repo_root = os.path.abspath(repo_root)
    graph_file = os.path.join(repo_root, ".mindgraph", "graph.json")
    if not os.path.exists(graph_file):
        raise FileNotFoundError(
            f"{graph_file} not found — first run mindgraph without --serve, "
            f"or together with it (it is built before the server is started)")
    return _ReusableTCPServer(("127.0.0.1", port), _make_handler(repo_root))


def run(repo_root: str, port: int = 5173, open_browser: bool = True) -> None:
    httpd = make_server(repo_root, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"viewer: {url}  (Ctrl+C to stop)")

    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
