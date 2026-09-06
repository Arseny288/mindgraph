"""Viewer static-server tests (serve.py).

They verify:
1. the viewer's static files live INSIDE the package and resolve via
   importlib.resources, not relative to the source file (otherwise
   ``pip install .`` without ``-e`` breaks ``--serve``);
2. the server serves index.html, all static assets and /graph.json with status
   200;
3. the caching hardening pass — content-hashed /dist/ assets, index.html/API
   always no-store, hashed assets cacheable forever, dev mode disables all of
   it, and no server ever answers a conditional GET with 304 for a no-store
   path.
"""

import glob
import json
import os
import re
import threading
import urllib.error
import urllib.request

import pytest

from mindgraph import serve, store


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    # Isolate the storage amendment's XDG root per test — graph.json now lives
    # there, never inside the repo (tmp_path stays a plain, empty "repo").
    monkeypatch.setenv("MINDGRAPH_DATA_HOME", str(tmp_path / "_data"))
    project = store.resolve_project(str(tmp_path))
    store._write_json(project.graph_path, {"version": 1, "repo": {"name": "test"}})
    return str(tmp_path)


@pytest.fixture()
def server(repo):
    httpd = serve.make_server(repo, port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def dev_server(repo):
    httpd = serve.make_server(repo, port=0, dev=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read(), dict(r.headers)


def _hashed_js_name():
    vdir = serve.viewer_dir()
    matches = glob.glob(os.path.join(vdir, "dist", "app.*.js"))
    assert matches, "no hashed dist/app.<hash>.js found — run `npm run build` in viewer/"
    return os.path.basename(matches[0])


def test_viewer_resources_are_inside_package():
    vdir = serve.viewer_dir()
    assert os.path.isdir(vdir), f"viewer_dir() is not a directory: {vdir!r}"
    assert "site-packages" in vdir or "mindgraph" in os.path.basename(
        os.path.dirname(vdir))
    for name in ("index.html", "main.jsx", "nodes.jsx", "theme.js"):
        assert os.path.isfile(os.path.join(vdir, name)), f"asset {name} missing"
    assert os.path.isfile(os.path.join(vdir, "dist", "reactflow.css"))
    assert re.match(r"^app\.[0-9a-f]{10}\.js$", _hashed_js_name())


def test_no_cdn_references_in_viewer():
    """0.2: the viewer must not pull react/reactflow/dagre from a CDN at runtime."""
    vdir = serve.viewer_dir()
    banned = ("esm.sh", "unpkg.com", "cdn.")
    for name in ("index.html", os.path.join("dist", _hashed_js_name())):
        content = open(os.path.join(vdir, name), encoding="utf-8").read()
        for token in banned:
            assert token not in content, f"{name} references a CDN: {token}"


def test_index_references_hashed_bundle_never_unhashed(server):
    status, body, _ = _get(server, "/")
    assert status == 200
    assert b"<title>mindgraph</title>" in body
    assert _hashed_js_name().encode() in body
    # item 1: index.html must never reference an unhashed bundle.
    assert b'src="/dist/app.js"' not in body


def test_static_assets_served(server):
    for name in ("main.jsx", "nodes.jsx", "theme.js",
                 f"dist/{_hashed_js_name()}", "dist/reactflow.css"):
        status, body, _ = _get(server, f"/{name}")
        assert status == 200, f"/{name} -> {status}"
        assert body, f"/{name} empty body"


def test_graph_json_served_from_repo(server):
    status, body, _ = _get(server, "/graph.json")
    assert status == 200
    data = json.loads(body)
    assert data["repo"]["name"] == "test"


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/no-such-file.jsx")
    assert exc.value.code == 404


# --- caching hardening pass ---------------------------------------------------


def test_cache_control_classification():
    hashed = f"/dist/{_hashed_js_name()}"
    assert serve.cache_control_for(hashed, dev=False) == "public, max-age=31536000, immutable"
    assert serve.cache_control_for("/index.html", dev=False) == "no-store"
    assert serve.cache_control_for("/", dev=False) == "no-store"
    assert serve.cache_control_for("/graph.json", dev=False) == "no-store"
    assert serve.cache_control_for("/api/state", dev=False) == "no-store"
    # Unhashed /dist/ files (fonts, vendored reactflow.css) are NOT the
    # immutable class — only genuinely hashed filenames are.
    assert serve.cache_control_for("/dist/reactflow.css", dev=False) == "no-store"
    assert serve.cache_control_for("/dist/fonts/whatever.woff2", dev=False) == "no-store"


def test_dev_mode_disables_all_caching():
    hashed = f"/dist/{_hashed_js_name()}"
    for path in (hashed, "/index.html", "/graph.json", "/api/state"):
        assert serve.cache_control_for(path, dev=True) == "no-store"


def test_index_html_is_no_store_on_every_request(server):
    for _ in range(3):
        status, _, headers = _get(server, "/")
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"


def test_hashed_asset_is_immutable_cache(server):
    status, _, headers = _get(server, f"/dist/{_hashed_js_name()}")
    assert status == 200
    assert headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_graph_json_is_no_store(server):
    status, _, headers = _get(server, "/graph.json")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_dev_server_hashed_asset_is_also_no_store(dev_server):
    status, _, headers = _get(dev_server, f"/dist/{_hashed_js_name()}")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_index_html_never_returns_304(server):
    """A conditional GET (as a browser would send on reload) must never get
    a 304 for index.html — that is precisely how a rebuilt page could keep
    showing stale content despite Cache-Control."""
    req = urllib.request.Request(f"http://127.0.0.1:{server}/",
                                 headers={"If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200


def test_hashed_asset_conditional_get_may_304():
    """Immutable assets are exempt from the conditional-header-stripping —
    a real 304 there is a harmless, useful optimization, not a bug."""
    assert serve.is_hashed_asset(f"/dist/{_hashed_js_name()}")


def test_spa_fallback_path_is_also_no_store(server):
    status, body, headers = _get(server, "/some/deep/link")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    assert b"<title>mindgraph</title>" in body


def test_read_build_id_matches_index_html(server):
    build_id = serve.read_build_id()
    assert build_id != "unknown"
    status, body, _ = _get(server, "/")
    assert f'content="{build_id}"'.encode() in body
