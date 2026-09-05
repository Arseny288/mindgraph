"""describe.py — build-time node/domain descriptions (item 7).

Uses a FakeProvider (no network, no API key) exercising the same
LLMProvider.complete(system, user, max_tokens) -> {"text", "usage"} contract
group.py's real providers implement.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mindgraph import describe

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_repo")


class FakeProvider:
    def __init__(self, text="this area does things.", fail_times=0, empty=False):
        self.text = text
        self.fail_times = fail_times
        self.empty = empty
        self.calls = 0

    def complete(self, system, user, max_tokens=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("provider unavailable")
        return {"text": "" if self.empty else self.text,
                "usage": {"input_tokens": 10, "output_tokens": 5}}


def _graph():
    return {
        "nodes": [{
            "id": "n_auth", "name": "auth", "parent": "d_root",
            "files": ["auth/login.py", "auth/tokens.py"],
            "symbols": ["auth.login.check_password"],
        }],
        "domains": [{"id": "d_root", "name": "Root"}],
        "node_edges": [], "domain_edges": [],
        "symbols": [{"id": "auth.login.check_password", "sig": "def check_password(u, p)",
                     "doc": "verify a password", "synthetic": False}],
    }


def test_node_content_hash_stable_and_content_sensitive(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    h1 = describe.node_content_hash(str(tmp_path), ["a.py"])
    h2 = describe.node_content_hash(str(tmp_path), ["a.py"])
    assert h1 == h2
    (tmp_path / "a.py").write_text("x = 2\n")
    h3 = describe.node_content_hash(str(tmp_path), ["a.py"])
    assert h3 != h1


def test_build_descriptions_generates_and_stamps_fields():
    graph = _graph()
    provider = FakeProvider(text="Handles login and token verification.")
    summary = describe.build_descriptions(FIXTURE, graph, provider, model_name="fake-1")

    node = graph["nodes"][0]
    dom = graph["domains"][0]
    assert node["description"] == "Handles login and token verification."
    assert node["description_model"] == "fake-1"
    assert node["description_hash"]
    assert node["description_generated_at"]
    assert dom["description"] == "Handles login and token verification."
    assert summary["total"] == 2
    assert summary["described"] == 2
    assert summary["reused"] == 0
    assert summary["failed"] == []
    assert summary["usage"]["input_tokens"] == 20  # 2 nodes x 10


def test_build_descriptions_reuses_unchanged_hash():
    graph1 = _graph()
    provider = FakeProvider()
    describe.build_descriptions(FIXTURE, graph1, provider, model_name="fake-1")
    assert provider.calls == 2

    graph2 = _graph()  # same files/content -> same hash
    summary2 = describe.build_descriptions(FIXTURE, graph2, provider, existing_graph=graph1,
                                           model_name="fake-1")
    assert provider.calls == 2  # no new calls — both reused
    assert summary2["reused"] == 2
    assert summary2["described"] == 0
    assert graph2["nodes"][0]["description"] == graph1["nodes"][0]["description"]


def test_build_descriptions_force_regenerates_even_if_unchanged():
    graph1 = _graph()
    provider = FakeProvider()
    describe.build_descriptions(FIXTURE, graph1, provider, model_name="fake-1")
    assert provider.calls == 2

    graph2 = _graph()
    summary2 = describe.build_descriptions(FIXTURE, graph2, provider, existing_graph=graph1,
                                           force=True, model_name="fake-1")
    assert provider.calls == 4
    assert summary2["reused"] == 0
    assert summary2["described"] == 2


def test_build_descriptions_never_raises_marks_null_and_reports_failure():
    graph = _graph()
    provider = FakeProvider(fail_times=999)  # always fails, all retries exhausted
    summary = describe.build_descriptions(FIXTURE, graph, provider, model_name="fake-1")

    assert graph["nodes"][0]["description"] is None
    assert graph["domains"][0]["description"] is None
    assert summary["described"] == 0
    assert len(summary["failed"]) == 2
    names = {n for n, _reason in summary["failed"]}
    assert names == {"auth", "Root"}
    # a hash is still recorded even on failure — a later unchanged run can at
    # least tell reuse vs regenerate apart once it succeeds.
    assert graph["nodes"][0]["description_hash"]
