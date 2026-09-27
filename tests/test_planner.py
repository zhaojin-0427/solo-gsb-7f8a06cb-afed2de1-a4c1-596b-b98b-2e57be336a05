"""Tests for the planner core and the HTTP API."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Node
from app.planner import PlanError, build_plan, topological_order

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def fp(content: str, deps: list[tuple[str, str]]) -> str:
    body = [content, [[name, f] for name, f in deps]]
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


# ------------------------------------------------------------- fingerprint


def test_fingerprint_matches_documented_formula():
    fa = fp("a", [])
    fb = fp("b", [("a", fa)])
    fc = fp("c", [("a", fa), ("b", fb)])

    plan = build_plan(
        [node("c", "c", ["b", "a"]), node("a", "a"), node("b", "b", ["a"])],
        {},
    )
    assert plan.plans["a"].fingerprint == fa
    assert plan.plans["b"].fingerprint == fb
    assert plan.plans["c"].fingerprint == fc


def test_dependency_list_order_does_not_change_fingerprint():
    p1 = build_plan([node("x", "x", ["a", "b"]), node("a", "a"), node("b", "b")], {})
    p2 = build_plan([node("b", "b"), node("x", "x", ["b", "a"]), node("a", "a")], {})
    assert p1.plans["x"].fingerprint == p2.plans["x"].fingerprint
    assert p1.plans["x"].dependencies == ["a", "b"]


def test_changed_dependency_fingerprint_propagates_downstream():
    n1 = [node("a", "v1"), node("b", "b", ["a"]), node("c", "c", ["b"])]
    n2 = [node("a", "v2"), node("b", "b", ["a"]), node("c", "c", ["b"])]
    before = build_plan(n1, {})
    after = build_plan(n2, {name: p.fingerprint for name, p in before.plans.items()})
    assert not after.plans["a"].cache_hit
    assert not after.plans["b"].cache_hit
    assert not after.plans["c"].cache_hit


def test_unchanged_dependency_keeps_downstream_fingerprint():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    first = build_plan(nodes, {})
    previous = {name: p.fingerprint for name, p in first.plans.items()}
    second = build_plan(
        [node("a", "a"), node("b", "b", ["a"]), node("x", "x")],
        {**previous},
    )
    assert second.plans["a"].cache_hit
    assert second.plans["b"].cache_hit
    assert not second.plans["x"].cache_hit


# ------------------------------------------------------------------ batches


def test_batches_group_independent_misses_together():
    # a hit; b,c depend only on a -> same batch; d depends on b,c -> next.
    fa = fp("a", [])
    fb = fp("b", [("a", fa)])
    fc = fp("c", [("a", fa)])
    fd = fp("d", [("b", fb), ("c", fc)])
    nodes = [
        node("a", "a"),
        node("b", "b", ["a"]),
        node("c", "c", ["a"]),
        node("d", "d", ["b", "c"]),
    ]
    plan = build_plan(nodes, {"a": fa})
    assert plan.batches == [["b", "c"], ["d"]]
    assert plan.plans["a"].batch is None and plan.plans["a"].cache_hit
    assert plan.plans["b"].batch == 0
    assert plan.plans["c"].batch == 0
    assert plan.plans["d"].batch == 1


def test_hit_node_still_participates_in_downstream_fingerprint():
    fa = fp("a", [])
    fb_good = fp("b", [("a", fa)])
    # Pretend b's stored fingerprint is wrong: it must be a miss even though
    # the formula depends only on hit node a.
    plan = build_plan(
        [node("a", "a"), node("b", "b", ["a"])],
        {"a": fa, "b": "deadbeef"},
    )
    assert plan.plans["a"].cache_hit
    assert not plan.plans["b"].cache_hit
    assert plan.plans["b"].fingerprint == fb_good
    assert plan.batches == [["b"]]


def test_all_hit_means_empty_batches():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    first = build_plan(nodes, {})
    previous = {name: p.fingerprint for name, p in first.plans.items()}
    second = build_plan(nodes, previous)
    assert second.batches == []
    assert all(p.cache_hit and p.batch is None for p in second.plans.values())


def test_empty_graph():
    plan = build_plan([], {})
    assert plan.plans == {}
    assert plan.batches == []


# ------------------------------------------------------------------ errors


def test_missing_dependency_error():
    with pytest.raises(PlanError) as exc:
        build_plan([node("a", "a", ["ghost"])], {})
    assert exc.value.code == "missing_dependency"
    assert exc.value.details["missing"] == {"a": ["ghost"]}


def test_cycle_error_reports_cycle_path():
    nodes = [
        node("a", "a", ["c"]),
        node("b", "b", ["a"]),
        node("c", "c", ["b"]),
    ]
    with pytest.raises(PlanError) as exc:
        build_plan(nodes, {})
    assert exc.value.code == "cycle_detected"
    cycle = exc.value.details["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b", "c"}


def test_self_loop_is_a_cycle():
    with pytest.raises(PlanError) as exc:
        build_plan([node("a", "a", ["a"])], {})
    assert exc.value.code == "cycle_detected"


def test_duplicate_node_name_error():
    with pytest.raises(PlanError) as exc:
        build_plan([node("a", "1"), node("a", "2")], {})
    assert exc.value.code == "duplicate_node"


def test_duplicate_dependency_name_rejected_by_model():
    with pytest.raises(ValueError):
        node("a", "a", ["b", "b"])


def test_topological_order_is_name_tie_broken():
    order = topological_order({"a": [], "b": [], "c": ["a", "b"]})
    assert order == ["a", "b", "c"]


# -------------------------------------------------------------- determinism


def test_input_order_does_not_affect_result():
    import random

    nodes = [
        node("core", "core"),
        node("util", "util", ["core"]),
        node("api", "api", ["util", "core"]),
        node("web", "web", ["api"]),
        node("docs", "docs"),
    ]
    baseline = build_plan(nodes, {"core": fp("core", [])}).model_dump()
    rng = random.Random(7)
    for _ in range(10):
        shuffled = nodes[:]
        for n in shuffled:
            rng.shuffle(n.dependency_names)
        rng.shuffle(shuffled)
        assert build_plan(shuffled, {"core": baseline["plans"]["core"]["fingerprint"]}).model_dump() == baseline


# ------------------------------------------------------------------- HTTP


def _payload(nodes, previous=None):
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
    }


def test_http_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_http_create_plan_status_and_body():
    resp = client.post("/plans", json=_payload([node("a", "a")]))
    assert resp.status_code == 201
    body = resp.json()
    assert set(body["plans"]) == {"a"}
    assert body["batches"] == [["a"]]


def test_http_missing_dependency_is_400():
    resp = client.post("/plans", json=_payload([node("a", "a", ["x"])]))
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "missing_dependency"
    assert body["details"]["missing"] == {"a": ["x"]}


def test_http_cycle_is_400():
    resp = client.post(
        "/plans",
        json=_payload([node("a", "a", ["b"]), node("b", "b", ["a"])]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


def test_http_validation_error_is_422():
    resp = client.post("/plans", json={"nodes": [{"name": "a"}]})
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    resp = client.post(
        "/plans",
        json={"nodes": [{"name": "a", "content_digest": "c", "bogus": 1}]},
    )
    assert resp.status_code == 422
