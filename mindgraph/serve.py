"""serve.py — the viewer's static server (§7 of the spec): no backend, no npm.

Serves files from ``viewer/`` as-is; the only exception is ``/graph.json``, which
is replaced with the requested repo's built graph — read from the tool's own
storage root (see :mod:`mindgraph.store`), never from inside the analyzed
repo. The viewer does not know about this — it just does ``fetch('/graph.json')``
relative to itself.

Caching (hardening pass): every request handler in this codebase — this
server AND studio.py's — shares the ONE cache-policy function below
(:func:`cache_control_for`) via its ``end_headers`` override, and the ONE
conditional-request function (:func:`prepare_conditional_headers`) via its
``do_GET``. Do not add a per-path Cache-Control special-case anywhere else;
that is exactly the "one behaviour implemented in several places, diverging"
bug this replaces (there used to be three independent ``.mindgraph`` path
computations — see store.py's docstring — this is the same class of bug).
The actual fix for staleness is upstream of all of this: build.mjs emits
content-hashed filenames (``app.<hash>.js``), so a changed bundle has a new
URL and no cache header can serve a stale one. Headers here are what makes
the *unhashed* shell (index.html, API responses) never cached at all, and
what lets the *hashed* assets be cached forever.
"""

from __future__ import annotations

import http.server
import importlib.resources
import os
import re
import socketserver
import threading
import webbrowser

from . import store

# Matches exactly the hashed filenames build.mjs writes: app.<10 hex>.js/css.
# Anything else under /dist/ (fonts, the vendored reactflow.css) is NOT
# content-hashed, so it must not get the long immutable cache — see item 1's
# note that only hashed assets are safe to cache "forever".
_HASHED_ASSET_RE = re.compile(r"^/dist/app\.[0-9a-f]{10}\.(js|css)$")


def is_hashed_asset(path: str) -> bool:
    return bool(_HASHED_ASSET_RE.match(path.split("?", 1)[0]))


def cache_control_for(path: str, dev: bool) -> str:
    """The single source of truth for Cache-Control, by request path.

    - dev mode: ``no-store`` for EVERYTHING (item 5's escape hatch — a
      rebuild must always be picked up even before/without the hash wired
      through some particular path).
    - a content-hashed ``/dist/app.<hash>.{js,css}``: cacheable forever —
      safe precisely because the filename changes when the content does.
    - everything else (index.html, the SPA-fallback shell, graph.json,
      /api/*, unhashed /dist/ files like fonts/reactflow.css): ``no-store``.
      index.html is the file that points at everything else, so it must
      never be cached; ``no-store`` (not ``no-cache``, which still stores
      and revalidates).
    """
    if not dev and is_hashed_asset(path):
        return "public, max-age=31536000, immutable"
    return "no-store"


def prepare_conditional_headers(handler: http.server.BaseHTTPRequestHandler, dev: bool) -> None:
    """Kill ETag/Last-Modified conditional-request games for every path in
    the ``no-store`` bucket above (dev mode: every path).

    ``http.server.SimpleHTTPRequestHandler.send_head()`` honours the
    request's ``If-Modified-Since``/``If-None-Match`` and can answer 304 for
    a file whose mtime looks unchanged — that is exactly how a rebuilt
    index.html could still serve a stale conditional hit. Stripping these
    from the INCOMING request before the base handler ever sees them is what
    stops that code path from ever firing 304, without needing to touch
    already-queued response headers (Last-Modified may still be sent, but
    with ``no-store`` on the response the browser must not use it for
    caching either way).
    """
    if not dev and is_hashed_asset(handler.path):
        return  # immutable — a real 304 here is a harmless, useful optimization
    for name in ("If-Modified-Since", "If-None-Match"):
        if name in handler.headers:
            del handler.headers[name]


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


_BUILD_ID_RE = re.compile(r'<meta name="mindgraph-build" content="([^"]*)"')


def read_build_id() -> str:
    """The build id (git sha + build timestamp) build.mjs stamped into the
    generated index.html — read back here rather than recomputed, so the
    id printed on server boot and the one in the served HTML can never
    drift apart (item 5's verification: the two must always match)."""
    try:
        with open(os.path.join(viewer_dir(), "index.html"), encoding="utf-8") as fh:
            head = fh.read(2048)
    except OSError:
        return "unknown"
    m = _BUILD_ID_RE.search(head)
    return m.group(1) if m else "unknown"


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
    handler.end_headers()  # picks up Cache-Control via the Handler's override
    handler.wfile.write(body)
    return True


def _make_handler(repo_root: str, dev: bool = False):
    graph_file = store.resolve_project(repo_root).graph_path

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
            prepare_conditional_headers(self, dev)
            if spa_fallback(self):
                return
            super().do_GET()

        def end_headers(self):
            # The ONE place Cache-Control is decided (see module docstring) —
            # covers every response path that calls end_headers(), which is
            # all of them: this static handler, and spa_fallback above.
            self.send_header("Cache-Control", cache_control_for(self.path, dev))
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # quiet — don't spam the console for every static file

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def make_server(repo_root: str, port: int = 5173, dev: bool = False):
    """Create (but do not start) the viewer's HTTP server for ``repo_root``.

    ``port=0`` gives a free OS port — the port can be read from
    ``server.server_address[1]``. Separated from :func:`run` for tests: the server
    can be started in a thread and cleanly stopped via ``shutdown()``.
    """
    repo_root = os.path.abspath(repo_root)
    graph_file = store.resolve_project(repo_root).graph_path
    if not os.path.exists(graph_file):
        raise FileNotFoundError(
            f"{graph_file} not found — first run mindgraph without --serve, "
            f"or together with it (it is built before the server is started)")
    return _ReusableTCPServer(("127.0.0.1", port), _make_handler(repo_root, dev=dev))


def run(repo_root: str, port: int = 5173, open_browser: bool = True, dev: bool = False) -> None:
    httpd = make_server(repo_root, port=port, dev=dev)
    url = f"http://127.0.0.1:{port}/"
    print(f"viewer: {url}  (Ctrl+C to stop)")
    print(f"mindgraph: build {read_build_id()}" + ("  [dev: no caching]" if dev else ""))

    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
