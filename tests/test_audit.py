"""Tests for the /execution-audits endpoint and its replay core."""
from __future__ import annotations

import hashlib
import json
import random

import pytest
from fastapi.testclient import TestClient

from app.audit import audit_execution
from app.main import app
from app.models import ExecutionEvent, Node
from app.planner import PlanError

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def fp(content: str, deps: list[tuple[str, str]]) -> str:
    body = [content, [[name, f] for name, f in deps]]
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def ev(seq: int, name: str, type_: str) -> ExecutionEvent:
    return ExecutionEvent(seq=seq, node=name, type=type_)


# ------------------------------------------------------------- cache seeds


def test_cache_hits_are_initially_completed():
    fa = fp("a", [])
    result = audit_execution([node("a", "a"), node("b", "b", ["a"])], {"a": fa}, [])
    assert result.states == {"a": "cached", "b": "pending"}
    assert result.violations == []


def test_extra_previous_fingerprint_keys_are_ignored():
    result = audit_execution([node("a", "a")], {"ghost": "deadbeef"}, [])
    assert result.states == {"a": "pending"}


def test_empty_graph_and_events():
    result = audit_execution([], {}, [])
    assert result.states == {}
    assert result.violations == []


# ------------------------------------------------------------------ replay


def test_happy_path_replay():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    events = [ev(0, "a", "start"), ev(1, "a", "success"), ev(2, "b", "start")]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "succeeded", "b": "running"}
    assert result.violations == []


def test_start_requires_completed_dependencies():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    events = [
        ev(0, "b", "start"),   # a still pending -> rejected
        ev(1, "a", "start"),
        ev(2, "b", "start"),   # a running, not completed -> rejected
        ev(3, "a", "success"),
        ev(4, "b", "start"),   # now legal
    ]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "succeeded", "b": "running"}
    assert [(v.seq, v.node, v.reason) for v in result.violations] == [
        (0, "b", "dependencies_incomplete"),
        (2, "b", "dependencies_incomplete"),
    ]


def test_cached_dependency_counts_as_completed():
    fa = fp("a", [])
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    result = audit_execution(nodes, {"a": fa}, [ev(0, "b", "start")])
    assert result.states == {"a": "cached", "b": "running"}
    assert result.violations == []


def test_success_and_failure_require_running():
    nodes = [node("a", "a"), node("b", "b"), node("c", "c")]
    events = [
        ev(0, "a", "success"),  # never started -> rejected
        ev(1, "b", "start"),
        ev(2, "b", "failure"),  # legal
        ev(3, "b", "success"),  # already ended -> rejected
        ev(4, "c", "failure"),  # never started -> rejected
    ]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "pending", "b": "failed", "c": "pending"}
    assert [(v.seq, v.node, v.reason) for v in result.violations] == [
        (0, "a", "not_running"),
        (3, "b", "not_running"),
        (4, "c", "not_running"),
    ]


def test_start_requires_pending_state():
    fa = fp("a", [])
    nodes = [node("a", "a"), node("b", "b"), node("c", "c"), node("d", "d")]
    events = [
        ev(0, "a", "start"),    # cached -> rejected
        ev(1, "b", "start"),
        ev(2, "b", "start"),    # already running -> rejected
        ev(3, "b", "success"),
        ev(4, "b", "start"),    # already succeeded -> rejected
        ev(5, "c", "start"),
        ev(6, "c", "failure"),
        ev(7, "c", "start"),    # already failed -> rejected
    ]
    result = audit_execution(nodes, {"a": fa}, events)
    assert result.states == {
        "a": "cached",
        "b": "succeeded",
        "c": "failed",
        "d": "pending",
    }
    assert [(v.seq, v.node, v.reason) for v in result.violations] == [
        (0, "a", "not_pending"),
        (2, "b", "not_pending"),
        (4, "b", "not_pending"),
        (7, "c", "not_pending"),
    ]


def test_violations_do_not_change_state():
    nodes = [node("a", "a")]
    events = [
        ev(0, "a", "success"),  # rejected, a stays pending
        ev(1, "a", "start"),    # still legal afterwards
        ev(2, "a", "success"),
    ]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "succeeded"}
    assert [(v.seq, v.reason) for v in result.violations] == [(0, "not_running")]


# ----------------------------------------------------------------- blocked


def test_failed_node_blocks_unfinished_successors_transitively():
    # a -> b -> c, plus d depending on b; a fails, nothing else can start.
    nodes = [
        node("a", "a"),
        node("b", "b", ["a"]),
        node("c", "c", ["b"]),
        node("d", "d", ["b"]),
        node("e", "e"),
    ]
    events = [ev(0, "a", "start"), ev(1, "a", "failure")]
    result = audit_execution(nodes, {}, events)
    assert result.states == {
        "a": "failed",
        "b": "blocked",
        "c": "blocked",
        "d": "blocked",
        "e": "pending",
    }


def test_blocked_successor_cannot_start():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    events = [
        ev(0, "a", "start"),
        ev(1, "a", "failure"),
        ev(2, "b", "start"),  # dep failed -> rejected
    ]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "failed", "b": "blocked"}
    assert [(v.seq, v.reason) for v in result.violations] == [
        (2, "dependencies_incomplete")
    ]


def test_succeeded_sibling_does_not_unblock():
    # b succeeded, c failed; d depends on both -> still blocked.
    nodes = [node("b", "b"), node("c", "c"), node("d", "d", ["b", "c"])]
    events = [
        ev(0, "b", "start"),
        ev(1, "b", "success"),
        ev(2, "c", "start"),
        ev(3, "c", "failure"),
    ]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"b": "succeeded", "c": "failed", "d": "blocked"}


def test_running_dependency_leaves_dependent_pending_not_blocked():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    result = audit_execution(nodes, {}, [ev(0, "a", "start")])
    assert result.states == {"a": "running", "b": "pending"}


def test_failed_node_with_no_dependents_reports_failed_only():
    nodes = [node("a", "a"), node("b", "b")]
    events = [ev(0, "a", "start"), ev(1, "a", "failure")]
    result = audit_execution(nodes, {}, events)
    assert result.states == {"a": "failed", "b": "pending"}


# -------------------------------------------------------------- determinism


def test_event_input_order_does_not_affect_result():
    nodes = [
        node("core", "core"),
        node("util", "util", ["core"]),
        node("api", "api", ["util", "core"]),
        node("web", "web", ["api"]),
    ]
    events = [
        ev(0, "core", "start"),
        ev(1, "core", "success"),
        ev(2, "util", "start"),
        ev(3, "util", "failure"),
        ev(4, "api", "start"),   # rejected: util failed
        ev(5, "web", "success"), # rejected: never started
        ev(6, "core", "start"),  # rejected: already succeeded
    ]
    baseline = audit_execution(nodes, {}, events).model_dump()
    rng = random.Random(11)
    for _ in range(10):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert audit_execution(nodes, {}, shuffled).model_dump() == baseline
    assert baseline["states"] == {
        "core": "succeeded",
        "util": "failed",
        "api": "blocked",
        "web": "blocked",
    }
    assert [v["seq"] for v in baseline["violations"]] == [4, 5, 6]


# ------------------------------------------------------------------- errors


def test_duplicate_seq_error():
    with pytest.raises(PlanError) as exc:
        audit_execution([node("a", "a")], {}, [ev(1, "a", "start"), ev(1, "a", "success")])
    assert exc.value.code == "duplicate_seq"
    assert exc.value.details["seq"] == [1]


def test_unknown_node_error():
    with pytest.raises(PlanError) as exc:
        audit_execution([node("a", "a")], {}, [ev(0, "ghost", "start")])
    assert exc.value.code == "unknown_node"
    assert exc.value.details["names"] == ["ghost"]


def test_graph_errors_are_reused():
    with pytest.raises(PlanError) as exc:
        audit_execution([node("a", "a", ["a"])], {}, [])
    assert exc.value.code == "cycle_detected"
    with pytest.raises(PlanError) as exc:
        audit_execution([node("a", "a", ["ghost"])], {}, [])
    assert exc.value.code == "missing_dependency"
    with pytest.raises(PlanError) as exc:
        audit_execution([node("a", "1"), node("a", "2")], {}, [])
    assert exc.value.code == "duplicate_node"


# --------------------------------------------------------------------- HTTP


def _payload(nodes, previous=None, events=None):
    return {
        "nodes": [
            {
                "name": n.name,
                "content_digest": n.content_digest,
                "dependency_names": n.dependency_names,
            }
            for n in nodes
        ],
        "previous_fingerprints": previous or {},
        "events": [
            e if isinstance(e, dict) else {"seq": e.seq, "node": e.node, "type": e.type}
            for e in (events or [])
        ],
    }


def test_http_audit_status_and_body():
    fa = fp("a", [])
    resp = client.post(
        "/execution-audits",
        json=_payload(
            [node("a", "a"), node("b", "b", ["a"])],
            previous={"a": fa},
            events=[ev(0, "b", "start"), ev(1, "b", "success")],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "states": {"a": "cached", "b": "succeeded"},
        "violations": [],
    }


def test_http_audit_violations_echo_event_type():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[ev(3, "a", "success")]),
    )
    assert resp.status_code == 200
    assert resp.json()["violations"] == [
        {"seq": 3, "node": "a", "type": "success", "reason": "not_running"}
    ]


def test_http_duplicate_seq_is_400():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[ev(0, "a", "start"), ev(0, "a", "start")]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_seq"


def test_http_unknown_node_is_400():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[ev(0, "ghost", "start")]),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "unknown_node"
    assert body["details"]["names"] == ["ghost"]


def test_http_cycle_is_400():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a", ["b"]), node("b", "b", ["a"])]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


def test_http_negative_seq_is_422():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[{"seq": -1, "node": "a", "type": "start"}]),
    )
    assert resp.status_code == 422


def test_http_unknown_event_type_is_422():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[{"seq": 0, "node": "a", "type": "skip"}]),
    )
    assert resp.status_code == 422


def test_http_unknown_field_is_422():
    resp = client.post(
        "/execution-audits",
        json=_payload([node("a", "a")], events=[{"seq": 0, "node": "a", "type": "start", "bogus": 1}]),
    )
    assert resp.status_code == 422
