"""Tests for the /cache-retention endpoint and its planning core."""
from __future__ import annotations

import hashlib
import json
import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Artifact, Node
from app.planner import PlanError, compute_fingerprints, validate_graph
from app.retention import MAX_CANDIDATES, plan_cache_retention

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def fp(content: str, deps: list[tuple[str, str]]) -> str:
    body = [content, [[name, f] for name, f in deps]]
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def fingerprints_of(nodes: list[Node]) -> dict[str, str]:
    graph, contents, order = validate_graph(nodes)
    return compute_fingerprints(graph, contents, order)


def artifact(name: str, fingerprint: str, size: int) -> Artifact:
    return Artifact(name=name, fingerprint=fingerprint, size=size)


def good_artifact(name: str, size: int, fps: dict[str, str]) -> Artifact:
    return artifact(name, fps[name], size)


# The README example graph: core <- util <- api <- web, plus standalone docs.
def readme_nodes() -> list[Node]:
    return [
        node("core", "core-src-v1"),
        node("util", "util-src-v1", ["core"]),
        node("api", "api-src-v1", ["util", "core"]),
        node("web", "web-src-v1", ["api"]),
        node("docs", "docs-src-v1"),
    ]


README_COSTS = {"core": 10, "util": 8, "api": 20, "web": 5, "docs": 3}


# --------------------------------------------------------------- selection


def test_readme_example_keeps_api_only():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        good_artifact("core", 4, fps),
        good_artifact("util", 6, fps),
        good_artifact("api", 10, fps),
        artifact("docs", "0" * 64, 2),  # stale fingerprint -> not a candidate
    ]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 14)
    # {api} and {core, api} both cost 5; ["api"] < ["api", "core"] wins.
    assert result.kept == ["api"]
    assert result.evicted == ["core", "docs", "util"]
    assert result.rebuild == ["web"]
    assert result.total_cost == 5


def test_kept_node_cuts_dependency_walk():
    # Chain a -> b -> c: keeping b hides c from the rebuild set entirely.
    nodes = [node("c", "c"), node("b", "b", ["c"]), node("a", "a", ["b"])]
    fps = fingerprints_of(nodes)
    costs = {"a": 5, "b": 8, "c": 10}
    artifacts = [good_artifact("b", 3, fps)]
    result = plan_cache_retention(nodes, "a", costs, artifacts, 3)
    assert result.kept == ["b"]
    assert result.rebuild == ["a"]
    assert result.total_cost == 5


def test_kept_node_does_not_hide_shared_direct_dependency():
    # README graph: api depends on core *directly* as well as via util, so
    # keeping util still leaves core on the rebuild path.
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [good_artifact("util", 6, fps)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 6)
    assert result.kept == ["util"]
    assert result.rebuild == ["api", "core", "web"]
    assert result.total_cost == 35


def test_shared_upstream_counted_once():
    # Diamond: app -> b, c -> base.  base must be charged a single time.
    nodes = [
        node("base", "base"),
        node("b", "b", ["base"]),
        node("c", "c", ["base"]),
        node("app", "app", ["b", "c"]),
    ]
    costs = {"base": 7, "b": 3, "c": 4, "app": 2}
    result = plan_cache_retention(nodes, "app", costs, [], 0)
    assert result.kept == []
    assert result.rebuild == ["app", "b", "base", "c"]
    assert result.total_cost == 16


def test_keeping_target_means_zero_cost():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [good_artifact("web", 1, fps)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 1)
    assert result.kept == ["web"]
    assert result.rebuild == []
    assert result.total_cost == 0


def test_tie_break_keeps_lexicographically_smaller_sorted_list():
    # "aaa" is useless for the target, but ["aaa", "lib"] < ["lib"], so the
    # tie between {lib} and {aaa, lib} keeps both.
    nodes = [node("lib", "lib"), node("app", "app", ["lib"]), node("aaa", "aaa")]
    fps = fingerprints_of(nodes)
    costs = {"lib": 9, "app": 1, "aaa": 5}
    artifacts = [good_artifact("lib", 5, fps), good_artifact("aaa", 3, fps)]
    result = plan_cache_retention(nodes, "app", costs, artifacts, 8)
    assert result.kept == ["aaa", "lib"]
    assert result.rebuild == ["app"]
    assert result.total_cost == 1


def test_tie_break_prefers_empty_list_when_costs_equal():
    # Keeping the (useless, zero-saving) artifact only makes the kept list
    # lexicographically larger, so it is evicted.
    nodes = [node("app", "app"), node("zzz", "zzz")]
    fps = fingerprints_of(nodes)
    costs = {"app": 4, "zzz": 6}
    artifacts = [good_artifact("zzz", 2, fps)]
    result = plan_cache_retention(nodes, "app", costs, artifacts, 2)
    assert result.kept == []
    assert result.evicted == ["zzz"]
    assert result.rebuild == ["app"]
    assert result.total_cost == 4


def test_artifact_larger_than_capacity_cannot_be_kept():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [good_artifact("api", 100, fps)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 10)
    assert result.kept == []
    assert result.evicted == ["api"]
    assert result.total_cost == 43


def test_zero_capacity_keeps_nothing():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [good_artifact("core", 1, fps)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 0)
    assert result.kept == []
    assert result.rebuild == ["api", "core", "util", "web"]
    assert result.total_cost == 43


def test_zero_rebuild_cost_is_allowed():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    result = plan_cache_retention(nodes, "b", {"a": 0, "b": 0}, [], 0)
    assert result.rebuild == ["a", "b"]
    assert result.total_cost == 0


def test_stale_artifact_is_evicted_and_rebuilt():
    nodes = readme_nodes()
    artifacts = [artifact("core", "f" * 64, 4)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 100)
    assert result.kept == []
    assert result.evicted == ["core"]
    assert result.total_cost == 43


def test_exact_fit_of_capacity_is_allowed():
    # size == capacity must be accepted (boundary is inclusive).
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [good_artifact("api", 10, fps)]
    result = plan_cache_retention(nodes, "web", README_COSTS, artifacts, 10)
    assert result.kept == ["api"]
    assert result.rebuild == ["web"]
    assert result.total_cost == 5


# ------------------------------------------------------------------ errors


def test_too_many_candidates_error():
    nodes = [node(f"n{i:02d}", f"c{i}") for i in range(MAX_CANDIDATES + 1)]
    fps = fingerprints_of(nodes)
    costs = {n.name: 1 for n in nodes}
    artifacts = [good_artifact(n.name, 1, fps) for n in nodes]
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(nodes, "n00", costs, artifacts, 100)
    assert exc.value.code == "too_many_candidates"
    assert exc.value.details["candidates"] == MAX_CANDIDATES + 1
    assert exc.value.details["limit"] == MAX_CANDIDATES


def test_stale_artifacts_do_not_count_towards_candidate_limit():
    nodes = [node(f"n{i:02d}", f"c{i}") for i in range(MAX_CANDIDATES + 1)]
    costs = {n.name: 1 for n in nodes}
    artifacts = [artifact(n.name, "0" * 64, 1) for n in nodes]  # all stale
    result = plan_cache_retention(nodes, "n00", costs, artifacts, 100)
    assert result.kept == []
    assert len(result.evicted) == MAX_CANDIDATES + 1


def test_duplicate_artifact_error():
    nodes = [node("a", "a")]
    artifacts = [artifact("a", "x", 1), artifact("a", "y", 2)]
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(nodes, "a", {"a": 1}, artifacts, 10)
    assert exc.value.code == "duplicate_artifact"
    assert exc.value.details["names"] == ["a"]


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        plan_cache_retention([node("a", "a")], "ghost", {"a": 1}, [], 0)
    assert exc.value.code == "unknown_target"
    assert exc.value.details["target"] == "ghost"


def test_unknown_artifact_error():
    nodes = [node("a", "a")]
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(nodes, "a", {"a": 1}, [artifact("ghost", "x", 1)], 0)
    assert exc.value.code == "unknown_artifact"
    assert exc.value.details["names"] == ["ghost"]


def test_unknown_rebuild_cost_error():
    nodes = [node("a", "a")]
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(nodes, "a", {"a": 1, "ghost": 2}, [], 0)
    assert exc.value.code == "unknown_rebuild_cost"
    assert exc.value.details["names"] == ["ghost"]


def test_missing_rebuild_cost_error():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(nodes, "b", {"b": 1}, [], 0)
    assert exc.value.code == "missing_rebuild_cost"
    assert exc.value.details["names"] == ["a"]


def test_graph_errors_are_reused():
    with pytest.raises(PlanError) as exc:
        plan_cache_retention(
            [node("a", "a", ["b"]), node("b", "b", ["a"])],
            "a",
            {"a": 1, "b": 1},
            [],
            0,
        )
    assert exc.value.code == "cycle_detected"

    with pytest.raises(PlanError) as exc:
        plan_cache_retention([node("a", "1"), node("a", "2")], "a", {"a": 1}, [], 0)
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        plan_cache_retention([node("a", "a", ["ghost"])], "a", {"a": 1}, [], 0)
    assert exc.value.code == "missing_dependency"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        good_artifact("core", 4, fps),
        good_artifact("util", 6, fps),
        good_artifact("api", 10, fps),
        artifact("docs", "0" * 64, 2),
    ]
    baseline = plan_cache_retention(
        nodes, "web", README_COSTS, artifacts, 14
    ).model_dump()
    rng = random.Random(23)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        shuffled_artifacts = artifacts[:]
        cost_items = list(README_COSTS.items())
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_artifacts)
        rng.shuffle(cost_items)
        result = plan_cache_retention(
            shuffled_nodes, "web", dict(cost_items), shuffled_artifacts, 14
        ).model_dump()
        assert result == baseline


# --------------------------------------------------------------------- HTTP


def _payload(nodes, target, costs, artifacts, capacity):
    return {
        "nodes": [
            {
                "name": n.name,
                "content_digest": n.content_digest,
                "dependency_names": n.dependency_names,
            }
            for n in nodes
        ],
        "target": target,
        "rebuild_costs": costs,
        "artifacts": [
            {"name": a.name, "fingerprint": a.fingerprint, "size": a.size}
            for a in artifacts
        ],
        "capacity": capacity,
    }


def _readme_payload(capacity=14):
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        good_artifact("core", 4, fps),
        good_artifact("util", 6, fps),
        good_artifact("api", 10, fps),
        artifact("docs", "0" * 64, 2),
    ]
    return _payload(nodes, "web", README_COSTS, artifacts, capacity)


def test_http_cache_retention_status_and_body():
    resp = client.post("/cache-retention", json=_readme_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "kept": ["api"],
        "evicted": ["core", "docs", "util"],
        "rebuild": ["web"],
        "total_cost": 5,
    }


def test_http_too_many_candidates_is_400():
    nodes = [node(f"n{i:02d}", f"c{i}") for i in range(MAX_CANDIDATES + 1)]
    fps = fingerprints_of(nodes)
    costs = {n.name: 1 for n in nodes}
    artifacts = [good_artifact(n.name, 1, fps) for n in nodes]
    resp = client.post(
        "/cache-retention", json=_payload(nodes, "n00", costs, artifacts, 100)
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "too_many_candidates"
    assert body["details"]["candidates"] == MAX_CANDIDATES + 1


def test_http_duplicate_artifact_is_400():
    nodes = [node("a", "a")]
    artifacts = [artifact("a", "x", 1), artifact("a", "y", 1)]
    resp = client.post(
        "/cache-retention", json=_payload(nodes, "a", {"a": 1}, artifacts, 1)
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_artifact"


def test_http_unknown_target_is_400():
    resp = client.post(
        "/cache-retention",
        json=_payload([node("a", "a")], "ghost", {"a": 1}, [], 0),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_unknown_artifact_is_400():
    resp = client.post(
        "/cache-retention",
        json=_payload(
            [node("a", "a")], "a", {"a": 1}, [artifact("ghost", "x", 1)], 0
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_artifact"


def test_http_cycle_is_400():
    nodes = [node("a", "a", ["b"]), node("b", "b", ["a"])]
    resp = client.post(
        "/cache-retention", json=_payload(nodes, "a", {"a": 1, "b": 1}, [], 0)
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


@pytest.mark.parametrize(
    "field, value",
    [
        ("capacity", -1),  # capacity must be non-negative
    ],
)
def test_http_invalid_capacity_is_422(field, value):
    payload = _readme_payload()
    payload[field] = value
    resp = client.post("/cache-retention", json=payload)
    assert resp.status_code == 422


def test_http_negative_rebuild_cost_is_422():
    payload = _readme_payload()
    payload["rebuild_costs"]["core"] = -1
    resp = client.post("/cache-retention", json=payload)
    assert resp.status_code == 422


def test_http_non_positive_artifact_size_is_422():
    payload = _readme_payload()
    payload["artifacts"][0]["size"] = 0
    resp = client.post("/cache-retention", json=payload)
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    payload = _readme_payload()
    payload["bogus"] = 1
    resp = client.post("/cache-retention", json=payload)
    assert resp.status_code == 422


def test_http_missing_section_rejected():
    payload = _readme_payload()
    del payload["capacity"]
    resp = client.post("/cache-retention", json=payload)
    assert resp.status_code == 422
