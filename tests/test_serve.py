"""Тесты статик-сервера вьюера (serve.py).

Проверяют два инварианта фазы 0.1:
1. статика вьюера лежит ВНУТРИ пакета и резолвится через importlib.resources,
   а не по пути относительно исходника (иначе ``pip install .`` без ``-e``
   ломает ``--serve``);
2. сервер отдаёт index.html, все статические ассеты и /graph.json со статусом
   200.
"""

import json
import os
import threading
import urllib.request

import pytest

from mindgraph import serve


@pytest.fixture()
def repo(tmp_path):
    graph_dir = tmp_path / ".mindgraph"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text(
        json.dumps({"version": 1, "repo": {"name": "test"}}), encoding="utf-8"
    )
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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read()


def test_viewer_resources_are_inside_package():
    vdir = serve.viewer_dir()
    assert os.path.isdir(vdir), f"viewer_dir() не каталог: {vdir!r}"
    assert "site-packages" in vdir or "mindgraph" in os.path.basename(
        os.path.dirname(vdir))
    for name in ("index.html", "main.jsx", "nodes.jsx", "theme.js"):
        assert os.path.isfile(os.path.join(vdir, name)), f"нет ассета {name}"


def test_index_served(server):
    status, body = _get(server, "/")
    assert status == 200
    assert b"<title>mindgraph</title>" in body


def test_static_assets_served(server):
    for name in ("main.jsx", "nodes.jsx", "theme.js"):
        status, body = _get(server, f"/{name}")
        assert status == 200, f"/{name} -> {status}"
        assert body, f"/{name} пустое тело"


def test_graph_json_served_from_repo(server):
    status, body = _get(server, "/graph.json")
    assert status == 200
    data = json.loads(body)
    assert data["repo"]["name"] == "test"


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/no-such-file.jsx")
    assert exc.value.code == 404
