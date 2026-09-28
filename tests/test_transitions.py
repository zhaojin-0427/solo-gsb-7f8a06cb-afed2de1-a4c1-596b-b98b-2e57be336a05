"""Tests for the /release-transitions endpoint and its planning core."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import BundleArtifact, Node
from app.planner import PlanError
from app.transitions import (
    MAX_ARTIFACTS_PER_NODE,
    MAX_CLOSURE_NODES,
    plan_release_transitions,
)

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=f"src-{name}", dependency_names=deps or [])


def artifact(
    id_: str,
    node_name: str,
    dependencies: dict[str, list[str]] | None = None,
) -> BundleArtifact:
    # Fingerprint/size are opaque metadata for transitions; any non-empty
    # fingerprint and positive size is accepted regardless of the graph.
    return BundleArtifact(
        id=id_,
        node=node_name,
        fingerprint=f"fp-{id_}",
        size=1,
        dependencies=dependencies or {},
    )


def chain_nodes() -> list[Node]:
    # README graph shape: core <- util <- api <- web, plus standalone docs.
    return [
        node("core"),
        node("util", ["core"]),
        node("api", ["util", "core"]),
        node("web", ["api"]),
        node("docs"),
    ]


def plan(nodes, target, artifacts, current, following):
    return plan_release_transitions(
        nodes, target, artifacts, list(current), list(following)
    )


def steps_as_pairs(result):
    return [(step.node, step.id) for step in result.steps]


def assert_bundles_cover_closure(result, closure):
    for step in result.steps:
        assert set(step.bundle) == closure
        assert len(step.bundle) == len(closure)


# ------------------------------------------------------------ basic cases


def test_identical_bundles_are_true_with_empty_steps():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
    ]
    result = plan(nodes, "b", artifacts, ["a1", "b1"], ["b1", "a1"])
    assert result.possible is True
    assert result.steps == []


def test_single_node_closure_switches_directly():
    nodes = [node("a")]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("a3", "a"),
    ]
    result = plan(nodes, "a", artifacts, ["a1"], ["a2"])
    assert result.possible is True
    assert steps_as_pairs(result) == [("a", "a2")]
    assert result.steps[0].bundle == {"a": "a2"}


def test_switch_each_node_once_when_order_is_free():
    # chain a <- b; every b artifact accepts both a ids, so the two nodes can
    # switch in either order; minimum length is 2 and (a, ...) is smaller.
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1", "a2"]}),
        artifact("b2", "b", {"a": ["a1", "a2"]}),
    ]
    result = plan(nodes, "b", artifacts, ["a1", "b1"], ["a2", "b2"])
    assert result.possible is True
    assert steps_as_pairs(result) == [("a", "a2"), ("b", "b2")]
    assert result.steps[0].bundle == {"a": "a2", "b": "b1"}
    assert result.steps[1].bundle == {"a": "a2", "b": "b2"}
    assert_bundles_cover_closure(result, {"a", "b"})


def test_transitional_artifact_is_required():
    # y1 accepts only x1, y2 only x2; the bridge yt accepts both.  The switch
    # has to move y to the bridge before x can change.
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("x2", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
        artifact("yt", "y", {"x": ["x1", "x2"]}),
        artifact("y2", "y", {"x": ["x2"]}),
    ]
    result = plan(nodes, "y", artifacts, ["x1", "y1"], ["x2", "y2"])
    assert result.possible is True
    assert steps_as_pairs(result) == [("y", "yt"), ("x", "x2"), ("y", "y2")]
    assert result.steps[0].bundle == {"x": "x1", "y": "yt"}
    assert result.steps[1].bundle == {"x": "x2", "y": "yt"}
    assert result.steps[2].bundle == {"x": "x2", "y": "y2"}


def test_transition_switching_one_node_twice_is_a_rollback():
    # Structure: ym accepts only x1; y2 accepts x2/xt; yb bridges x1/xt/x2.
    # The endpoints (x1, ym) and (x2, y2) are consistent, but reaching the
    # goal forces node y to switch twice (ym -> yb -> y2), i.e. the old
    # artifact at y is abandoned through a transitional one (a rollback-style
    # detour) rather than replacing each node just once.
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("xt", "x"),
        artifact("x2", "x"),
        artifact("ym", "y", {"x": ["x1"]}),
        artifact("yb", "y", {"x": ["x1", "xt", "x2"]}),
        artifact("y2", "y", {"x": ["x2", "xt"]}),
    ]
    result = plan(nodes, "y", artifacts, ["x1", "ym"], ["x2", "y2"])
    pairs = steps_as_pairs(result)
    assert result.possible is True
    # Shortest consistent route (x cannot move first: ym rejects xt/x2):
    #   (x1,ym) -> (x1,yb) -> (x2,yb) -> (x2,y2)
    # Node y changes twice and node x once; 3 steps is minimal since each
    # endpoint id differs from the start and x cannot switch before y moves.
    assert pairs == [("y", "yb"), ("x", "x2"), ("y", "y2")]
    switched = [pair[0] for pair in pairs]
    assert switched.count("y") == 2
    # Every intermediate bundle covers the closure and is dependency-consistent.
    assert_bundles_cover_closure(result, {"x", "y"})
    by_id = {a.id: a for a in artifacts}
    graph = {"x": [], "y": ["x"]}
    for step in result.steps:
        for n_name, chosen in step.bundle.items():
            for dep in graph[n_name]:
                assert step.bundle[dep] in by_id[chosen].dependencies[dep]


def test_no_possible_transition_when_components_disconnected():
    # y1 only accepts x1, y2 only accepts x2; with no bridge artifact the
    # compatibility components never connect.
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("x2", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
        artifact("y2", "y", {"x": ["x2"]}),
    ]
    result = plan(nodes, "y", artifacts, ["x1", "y1"], ["x2", "y2"])
    assert result.possible is False
    assert result.steps == []


def test_single_step_node_order_tie_prefers_smaller_node():
    # Two nodes differ between start and goal and either can switch first
    # while keeping the bundle consistent; node 'a' must win the tie.
    nodes = [
        node("base"),
        node("a", ["base"]),
        node("b", ["base"]),
        node("app", ["a", "b"]),
    ]
    artifacts = [
        artifact("base1", "base"),
        artifact("a1", "a", {"base": ["base1"]}),
        artifact("a2", "a", {"base": ["base1"]}),
        artifact("b1", "b", {"base": ["base1"]}),
        artifact("b2", "b", {"base": ["base1"]}),
        artifact(
            "app1",
            "app",
            {"a": ["a1", "a2"], "b": ["b1", "b2"]},
        ),
        artifact(
            "app2",
            "app",
            {"a": ["a1", "a2"], "b": ["b1", "b2"]},
        ),
    ]
    # Only a and b actually change (app stays on app1), both moves free.
    result = plan(
        nodes, "app", artifacts,
        ["base1", "a1", "b1", "app1"],
        ["base1", "a2", "b2", "app1"],
    )
    assert result.possible is True
    pairs = steps_as_pairs(result)
    assert len(pairs) == 2
    assert pairs[0][0] == "a"
    assert pairs == [("a", "a2"), ("b", "b2")]


def test_same_node_id_tie_prefers_smaller_id_at_later_position():
    # First step is forced to node b.  Afterwards two equally short routes
    # differ only in the id chosen at a second step on the same node a:
    # both converge to the goal at the same length.
    nodes = [
        node("a"),
        node("b", ["a"]),
    ]
    artifacts = [
        artifact("a1", "a"),
        artifact("am", "a"),
        artifact("a2", "a"),
        # b1 (start) accepts only a1; b2 (goal) accepts only a2.  Two bridges
        # bm and bz both accept every a id.  After the forced first step
        # b1 -> bm vs b1 -> bz, node 'b' id 'bm' < 'bz' decides the FIRST
        # step; to create an id tie at a *later* position sharing the same
        # node, use a single bridge and two intermediate a ids (am, a2) that
        # both reach the goal in the remaining steps.
        artifact("b1", "b", {"a": ["a1"]}),
        artifact("bm", "b", {"a": ["a1", "am", "a2"]}),
        artifact("b2", "b", {"a": ["a2"]}),
    ]
    result = plan(nodes, "b", artifacts, ["a1", "b1"], ["a2", "b2"])
    pairs = steps_as_pairs(result)
    assert result.possible is True
    # Shortest consistent sequence: b1 -> bm, then a1 -> a2, then bm -> b2.
    assert pairs == [("b", "bm"), ("a", "a2"), ("b", "b2")]


def test_lexicographic_tie_on_second_step_node():
    # Forced first step on node z; afterwards a switch on node a and one on
    # node m are both required and interchangeable -> 'a' first.
    nodes = [
        node("a"),
        node("m"),
        node("z", ["a", "m"]),
    ]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("m1", "m"),
        artifact("m2", "m"),
        artifact("z1", "z", {"a": ["a1"], "m": ["m1"]}),
        # bridge accepts every combination reachable during the switch
        artifact(
            "zt",
            "z",
            {"a": ["a1", "a2"], "m": ["m1", "m2"]},
        ),
        artifact("z2", "z", {"a": ["a2"], "m": ["m2"]}),
    ]
    result = plan(
        nodes, "z", artifacts,
        ["a1", "m1", "z1"], ["a2", "m2", "z2"],
    )
    assert result.possible is True
    pairs = steps_as_pairs(result)
    assert pairs[0] == ("z", "zt")
    # z bridge must come first (z1 rejects the new ids); after it, a < m.
    assert pairs[1] == ("a", "a2")
    assert pairs[2] == ("m", "m2")
    assert pairs[3] == ("z", "z2")
    assert len(pairs) == 4


def test_intermediate_bundle_must_remain_consistent():
    # b accepts x1/x2 both, but the switch a->b ordering must still respect
    # edges: craft a case where switching node x first breaks its dependent y
    # (y1 accepts only x1), so even though both nodes change, y goes first via
    # a bridge; the planner output's every intermediate bundle is checked.
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("x2", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
        artifact("yt", "y", {"x": ["x1", "x2"]}),
        artifact("y2", "y", {"x": ["x2"]}),
    ]
    result = plan(nodes, "y", artifacts, ["x1", "y1"], ["x2", "y2"])
    # Independently verify consistency of each reported bundle.
    by_id = {a.id: a for a in artifacts}
    graph = {"x": [], "y": ["x"]}
    for step in result.steps:
        for n, chosen in step.bundle.items():
            for dep in graph[n]:
                assert step.bundle[dep] in by_id[chosen].dependencies[dep]


def test_exactly_three_artifacts_per_node_is_allowed():
    nodes = [node("a")]
    artifacts = [artifact("a1", "a"), artifact("a2", "a"), artifact("a3", "a")]
    result = plan(nodes, "a", artifacts, ["a1"], ["a3"])
    assert result.possible is True
    assert steps_as_pairs(result) == [("a", "a3")]


# ------------------------------------------------------- closure & counts


def test_closure_limit_is_eight_nodes():
    # chain of 9: n0 <- n1 <- ... <- n8, target n8 -> closure size 9.
    names = [f"n{i}" for i in range(MAX_CLOSURE_NODES + 1)]
    nodes = [node(names[0])] + [
        node(names[i], [names[i - 1]]) for i in range(1, len(names))
    ]
    artifacts = []
    for i, name in enumerate(names):
        deps = {names[i - 1]: [f"a-{names[i - 1]}"]} if i else {}
        artifacts.append(artifact(f"a-{name}", name, deps))
    with pytest.raises(PlanError) as exc:
        plan(nodes, names[-1], artifacts, [f"a-{n}" for n in names],
             [f"a-{n}" for n in names])
    assert exc.value.code == "closure_too_large"
    assert exc.value.details == {
        "nodes": MAX_CLOSURE_NODES + 1,
        "limit": MAX_CLOSURE_NODES,
    }


def test_exactly_eight_closure_nodes_is_allowed():
    names = [f"n{i}" for i in range(MAX_CLOSURE_NODES)]
    nodes = [node(names[0])] + [
        node(names[i], [names[i - 1]]) for i in range(1, len(names))
    ]
    artifacts = []
    for i, name in enumerate(names):
        deps = {names[i - 1]: [f"a-{names[i - 1]}"]} if i else {}
        artifacts.append(artifact(f"a-{name}", name, deps))
    ids = [f"a-{n}" for n in names]
    result = plan(nodes, names[-1], artifacts, ids, ids)
    assert result.possible is True
    assert result.steps == []


def test_zero_artifacts_for_a_closure_node_is_error():
    # Three-node closure targeting b -> {a, b}; node c is unrelated and not
    # in the closure.  Offering a1/b1 only leaves a closure node count of 0
    # for neither here, so use a two-node target closure plus an extra node
    # 'b2-unused' -- instead, directly: target app closure {base, app} and
    # only base receives an artifact (app gets none).  References are absent
    # too, but the per-node count check fires before reference validation.
    nodes = [node("base"), node("app", ["base"]), node("docs")]
    artifacts = [artifact("base1", "base")]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "app", artifacts, ["base1"], ["base1"])
    assert exc.value.code == "invalid_artifact_count"
    assert exc.value.details["nodes"] == [{"node": "app", "artifacts": 0}]


def test_more_than_three_artifacts_for_node_is_error():
    nodes = [node("a")]
    artifacts = [artifact(f"a{i}", "a") for i in range(MAX_ARTIFACTS_PER_NODE + 1)]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "a", artifacts, ["a0"], ["a0"])
    assert exc.value.code == "invalid_artifact_count"
    assert exc.value.details == {
        "nodes": [{"node": "a", "artifacts": 4}],
        "min": 1,
        "max": 3,
    }


# ------------------------------------------------------------------ errors


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        plan([node("a")], "ghost", [], [], [])
    assert exc.value.code == "unknown_target"
    assert exc.value.details == {"target": "ghost"}


def test_duplicate_artifact_id_error():
    nodes = [node("a")]
    artifacts = [artifact("same", "a"), artifact("same", "a")]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "a", artifacts, ["same"], ["same"])
    assert exc.value.code == "duplicate_artifact_id"
    assert exc.value.details == {"ids": ["same"]}


def test_unknown_artifact_node_error():
    nodes = [node("a")]
    artifacts = [
        artifact("a1", "a"),
        BundleArtifact(
            id="x", node="ghost", fingerprint="f", size=1, dependencies={}
        ),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "a", artifacts, ["a1"], ["a1"])
    assert exc.value.code == "unknown_artifact_node"
    assert exc.value.details == {"names": ["ghost"]}


def test_artifact_outside_closure_error():
    nodes = chain_nodes()
    artifacts = [
        artifact("core1", "core"),
        artifact("util1", "util", {"core": ["core1"]}),
        artifact(
            "api1", "api", {"core": ["core1"], "util": ["util1"]}
        ),
        artifact("web1", "web", {"api": ["api1"]}),
        artifact("docs1", "docs"),  # not reachable from web
    ]
    current = ["core1", "util1", "api1", "web1"]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "web", artifacts, current, current)
    assert exc.value.code == "artifact_outside_closure"
    assert exc.value.details == {"ids": ["docs1"]}


def test_dependency_map_mismatch_error():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {}),  # missing the 'a' key
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["a1", "b1"], ["a1", "b1"])
    assert exc.value.code == "invalid_dependency_map"
    assert exc.value.details == {
        "artifacts": [{"id": "b1", "missing": ["a"]}]
    }


def test_unknown_artifact_reference_error():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["ghost"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["a1", "b1"], ["a1", "b1"])
    assert exc.value.code == "unknown_artifact_reference"
    assert exc.value.details == {"ids": ["ghost"]}


def test_wrong_node_reference_error():
    # Chain a <- b <- c so every node is in c's closure; b1 lists c1 (which
    # belongs to c) as acceptable for its dependency a.
    nodes = [node("a"), node("b", ["a"]), node("c", ["b"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["c1"]}),
        artifact("c1", "c", {"b": ["b1"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "c", artifacts, ["a1", "b1", "c1"], ["a1", "b1", "c1"])
    assert exc.value.code == "invalid_artifact_reference"
    assert exc.value.details == {
        "references": [
            {"artifact": "b1", "dependency": "a", "artifact_id": "c1"}
        ]
    }


def test_endpoint_unknown_id_error():
    nodes = [node("a")]
    artifacts = [artifact("a1", "a")]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "a", artifacts, ["ghost"], ["a1"])
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details == {"endpoint": "current", "unknown_ids": ["ghost"]}


def test_endpoint_duplicate_id_error():
    # The endpoint may legitimately list an artifact twice only when it
    # belongs to a single node; with one artifact per node the duplicated id
    # a1 both covers node a (twice) and leaves node b uncovered.  Duplicate
    # detection precedes coverage detection.
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["a1", "a1", "b1"], ["a1", "b1"])
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details == {
        "endpoint": "current",
        "duplicate_ids": ["a1"],
    }


def test_endpoint_missing_node_error():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["b1"], ["a1", "b1"])
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details == {
        "endpoint": "current",
        "missing_nodes": ["a"],
    }


def test_endpoint_two_ids_for_same_node_error():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1", "a2"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["a1", "a2", "b1"], ["a1", "b1"])
    assert exc.value.code == "invalid_endpoint_bundle"
    details = exc.value.details
    assert details["endpoint"] == "current"
    assert details["extra_nodes"] == ["a"]


def test_endpoint_inconsistent_dependency_error():
    # Endpoint bundle chooses b1 which accepts only a2, but selects a1.
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a2"]}),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "b", artifacts, ["a1", "b1"], ["a1", "b1"])
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details == {
        "endpoint": "current",
        "inconsistencies": [
            {"node": "b", "dependency": "a", "artifact_id": "b1"}
        ],
    }


def test_next_endpoint_validated_after_current():
    nodes = [node("a")]
    artifacts = [artifact("a1", "a")]
    with pytest.raises(PlanError) as exc:
        plan(nodes, "a", artifacts, ["a1"], ["ghost"])
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details["endpoint"] == "next"


def test_graph_errors_are_reused():
    artifacts = [artifact("a1", "a"), artifact("b1", "b", {"a": ["a1"]})]
    with pytest.raises(PlanError) as exc:
        plan([node("a", ["b"]), node("b", ["a"])], "a", artifacts, [], [])
    assert exc.value.code == "cycle_detected"

    with pytest.raises(PlanError) as exc:
        plan([node("a"), node("a")], "a", [], [], [])
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        plan([node("a", ["ghost"])], "a", [], [], [])
    assert exc.value.code == "missing_dependency"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    rng = random.Random(1729)
    nodes = [
        node("base"),
        node("x", ["base"]),
        node("y", ["base"]),
        node("app", ["x", "y"]),
    ]
    artifacts = [
        artifact("base1", "base"),
        artifact("x1", "x", {"base": ["base1"]}),
        artifact("xt", "x", {"base": ["base1"]}),
        artifact("x2", "x", {"base": ["base1"]}),
        artifact("y1", "y", {"base": ["base1"]}),
        artifact("yt", "y", {"base": ["base1"]}),
        artifact("y2", "y", {"base": ["base1"]}),
        artifact(
            "app1",
            "app",
            {"x": ["x1", "xt", "x2"], "y": ["y1", "yt", "y2"]},
        ),
        artifact(
            "app2",
            "app",
            {"x": ["x1", "xt", "x2"], "y": ["y1", "yt", "y2"]},
        ),
    ]
    current = ["base1", "x1", "y1", "app1"]
    following = ["base1", "x2", "y2", "app2"]
    baseline = plan(nodes, "app", artifacts, current, following).model_dump()
    for _ in range(10):
        shuffled_nodes = nodes[:]
        shuffled_artifacts = artifacts[:]
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_artifacts)
        result = plan(
            shuffled_nodes,
            "app",
            shuffled_artifacts,
            reversed(current),
            reversed(following),
        ).model_dump()
        assert result == baseline


def test_endpoint_id_list_order_does_not_matter():
    nodes = [node("a"), node("b", ["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
    ]
    r1 = plan(nodes, "b", artifacts, ["a1", "b1"], ["a1", "b1"])
    r2 = plan(nodes, "b", artifacts, ["b1", "a1"], ["b1", "a1"])
    assert r1.model_dump() == r2.model_dump()


# --------------------------------------------------------------------- HTTP


def _payload(nodes, target, artifacts, current, following):
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
        "artifacts": [
            {
                "id": a.id,
                "node": a.node,
                "fingerprint": a.fingerprint,
                "size": a.size,
                "dependencies": a.dependencies,
            }
            for a in artifacts
        ],
        "current_ids": list(current),
        "next_ids": list(following),
    }


def test_http_transition_possible():
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("x2", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
        artifact("yt", "y", {"x": ["x1", "x2"]}),
        artifact("y2", "y", {"x": ["x2"]}),
    ]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, "y", artifacts, ["x1", "y1"], ["y2", "x2"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "possible": True,
        "steps": [
            {"node": "y", "id": "yt", "bundle": {"x": "x1", "y": "yt"}},
            {"node": "x", "id": "x2", "bundle": {"x": "x2", "y": "yt"}},
            {"node": "y", "id": "y2", "bundle": {"x": "x2", "y": "y2"}},
        ],
    }


def test_http_transition_identical():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x")]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, "x", artifacts, ["x1"], ["x1"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {"possible": True, "steps": []}


def test_http_transition_impossible():
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("x2", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
        artifact("y2", "y", {"x": ["x2"]}),
    ]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, "y", artifacts, ["x1", "y1"], ["x2", "y2"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {"possible": False, "steps": []}


def test_http_unknown_target_is_400():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x")]
    payload = _payload(nodes, "ghost", artifacts, ["x1"], ["x1"])
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_duplicate_artifact_id_is_400():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x"), artifact("x1", "x")]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, "x", artifacts, ["x1"], ["x1"]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_artifact_id"


def test_http_closure_too_large_is_400():
    names = [f"n{i}" for i in range(MAX_CLOSURE_NODES + 1)]
    nodes = [node(names[0])] + [
        node(names[i], [names[i - 1]]) for i in range(1, len(names))
    ]
    artifacts = []
    for i, name in enumerate(names):
        deps = {names[i - 1]: [f"a-{names[i - 1]}"]} if i else {}
        artifacts.append(artifact(f"a-{name}", name, deps))
    ids = [f"a-{n}" for n in names]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, names[-1], artifacts, ids, ids),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "closure_too_large"


def test_http_artifact_outside_closure_is_400():
    resp = client.post(
        "/release-transitions",
        json=_payload(
            chain_nodes(),
            "docs",
            [
                artifact("core1", "core"),
                artifact("docs1", "docs"),
            ],
            ["docs1"],
            ["docs1"],
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "artifact_outside_closure"


def test_http_invalid_endpoint_bundle_is_400():
    nodes = [node("x"), node("y", ["x"])]
    artifacts = [
        artifact("x1", "x"),
        artifact("y1", "y", {"x": ["x1"]}),
    ]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, "y", artifacts, ["y1"], ["x1", "y1"]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_endpoint_bundle"
    assert resp.json()["details"]["endpoint"] == "current"


def test_http_cycle_is_400():
    payload = _payload(
        [
            Node(name="a", content_digest="a", dependency_names=["b"]),
            Node(name="b", content_digest="b", dependency_names=["a"]),
        ],
        "a",
        [
            artifact("a1", "a", {"b": ["b1"]}),
            artifact("b1", "b", {"a": ["a1"]}),
        ],
        ["a1", "b1"],
        ["a1", "b1"],
    )
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


def test_http_unknown_field_rejected():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x")]
    payload = _payload(nodes, "x", artifacts, ["x1"], ["x1"])
    payload["bogus"] = 1
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422


def test_http_missing_endpoint_list_rejected():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x")]
    payload = _payload(nodes, "x", artifacts, ["x1"], ["x1"])
    del payload["next_ids"]
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422


def test_http_empty_endpoint_list_rejected():
    nodes = [node("x")]
    artifacts = [artifact("x1", "x")]
    payload = _payload(nodes, "x", artifacts, [], ["x1"])
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422
