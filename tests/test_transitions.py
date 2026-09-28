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
    plan_release_transition,
)

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, content: str = "x", deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def artifact(
    id_: str,
    node_name: str,
    dependencies: dict[str, list[str]] | None = None,
) -> BundleArtifact:
    # Fingerprints are metadata for transitions: the same opaque value on
    # every artifact must never gate selection.
    return BundleArtifact(
        id=id_,
        node=node_name,
        fingerprint="opaque-fingerprint",
        size=1,
        dependencies=dependencies or {},
    )


def chain_nodes() -> list[Node]:
    # base <- svc <- web
    return [
        node("base", "b"),
        node("svc", "s", ["base"]),
        node("web", "w", ["svc"]),
    ]


def chain_artifacts() -> list[BundleArtifact]:
    return [
        artifact("b1", "base"),
        artifact("b2", "base"),
        artifact("s1", "svc", {"base": ["b1"]}),
        artifact("s2", "svc", {"base": ["b2"]}),
        # s3 is a transitional build accepting either base artifact.
        artifact("s3", "svc", {"base": ["b1", "b2"]}),
        artifact("w1", "web", {"svc": ["s1"]}),
        artifact("w2", "web", {"svc": ["s2"]}),
        # w3 accepts every svc build and bridges the two endpoint bundles.
        artifact("w3", "web", {"svc": ["s1", "s2", "s3"]}),
    ]


CHAIN_CURRENT = ["b1", "s1", "w1"]
CHAIN_NEXT = ["b2", "s2", "w2"]


def _bundle_is_consistent(
    nodes: list[Node], artifacts: list[BundleArtifact], ids: list[str]
) -> bool:
    graph = {n.name: list(n.dependency_names) for n in nodes}
    by_id = {a.id: a for a in artifacts}
    assignment = {by_id[i].node: i for i in ids}
    return all(
        assignment[dep] in by_id[rid].dependencies[dep]
        for rid in ids
        for dep in graph[by_id[rid].node]
    )


# -------------------------------------------------------------- happy paths


def test_identical_bundles_need_no_steps():
    result = plan_release_transition(
        chain_nodes(), "web", chain_artifacts(), CHAIN_CURRENT, CHAIN_CURRENT
    )
    assert result.possible is True
    assert result.steps == []


def test_single_node_switch():
    nodes = [node("a"), node("b", deps=["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1", "a2"]}),
    ]
    result = plan_release_transition(
        nodes, "b", artifacts, ["a1", "b1"], ["a2", "b1"]
    )
    assert result.possible is True
    assert [(s.node, s.id) for s in result.steps] == [("a", "a2")]
    assert result.steps[0].bundle == {"a": "a2", "b": "b1"}


def test_shortest_path_uses_transitional_artifacts():
    result = plan_release_transition(
        chain_nodes(), "web", chain_artifacts(), CHAIN_CURRENT, CHAIN_NEXT
    )
    assert result.possible is True
    moves = [(s.node, s.id) for s in result.steps]
    # web must move onto the bridge w3 first; svc passes through the
    # transitional s3 while base flips underneath it.
    assert moves == [
        ("web", "w3"),
        ("svc", "s3"),
        ("base", "b2"),
        ("svc", "s2"),
        ("web", "w2"),
    ]


def test_every_step_changes_one_node_and_lands_in_a_consistent_bundle():
    nodes = chain_nodes()
    artifacts = chain_artifacts()
    result = plan_release_transition(
        nodes, "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
    )
    assert result.possible is True
    running = dict(zip(["base", "svc", "web"], CHAIN_CURRENT))
    previous = dict(running)
    for step in result.steps:
        assert running[step.node] != step.id
        running[step.node] = step.id
        assert step.bundle == running
        assert _bundle_is_consistent(nodes, artifacts, list(running.values()))
        previous = running
    assert running == {"base": "b2", "svc": "s2", "web": "w2"}


def test_unreachable_endpoint_is_not_possible():
    nodes = [node("a"), node("b", deps=["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
        artifact("b2", "b", {"a": ["a2"]}),
    ]
    result = plan_release_transition(
        nodes, "b", artifacts, ["a1", "b1"], ["a2", "b2"]
    )
    assert result.possible is False
    assert result.steps == []


def test_independent_nodes_can_switch_in_any_order_and_lex_order_wins():
    # t accepts every combination of the a/b artifacts; both orderings take
    # two steps, so the (node, id) sequence starting with node a wins.
    nodes = [node("a"), node("b"), node("t", deps=["a", "b"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b"),
        artifact("b2", "b"),
        artifact("t1", "t", {"a": ["a1", "a2"], "b": ["b1", "b2"]}),
    ]
    result = plan_release_transition(
        nodes, "t", artifacts, ["a1", "b1", "t1"], ["a2", "b2", "t1"]
    )
    assert result.possible is True
    assert [(s.node, s.id) for s in result.steps] == [
        ("a", "a2"),
        ("b", "b2"),
    ]
    assert result.steps[-1].bundle == {"a": "a2", "b": "b2", "t": "t1"}


def test_lex_tie_break_compares_full_step_sequence():
    # Two length-three routes meet at a common state after two moves:
    # (a),(x) vs (b),(c) then a shared final move.  "a" < "b", so the
    # first route must be reported.
    nodes = [
        node("a"),
        node("b"),
        node("c"),
        node("x"),
        node("t", deps=["a", "b", "c", "x"]),
    ]

    def free(name: str, alt: str) -> list[BundleArtifact]:
        return [artifact(f"{name}1", name), artifact(alt, name)]

    artifacts = [
        *free("a", "a2"),
        *free("b", "b2"),
        *free("c", "c2"),
        *free("x", "x2"),
        artifact(
            "t1",
            "t",
            {
                "a": ["a1", "a2"],
                "b": ["b1", "b2"],
                "c": ["c1", "c2"],
                "x": ["x1", "x2"],
            },
        ),
    ]
    result = plan_release_transition(
        nodes,
        "t",
        artifacts,
        ["a1", "b1", "c1", "x1", "t1"],
        ["a2", "b2", "c2", "x2", "t1"],
    )
    assert result.possible is True
    moves = [(s.node, s.id) for s in result.steps]
    assert moves == [
        ("a", "a2"),
        ("b", "b2"),
        ("c", "c2"),
        ("x", "x2"),
    ]


def test_rollback_and_artifact_revisits_are_allowed_but_not_needed():
    # Both endpoints agree on b1; the only route out and back passes b2.
    # The optimal plan never revisits anything here; this just confirms the
    # search explores the transitional artifact in both directions.
    nodes = [node("a"), node("b", deps=["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
        artifact("b2", "b", {"a": ["a1", "a2"]}),
    ]
    result = plan_release_transition(
        nodes, "b", artifacts, ["a1", "b1"], ["a2", "b2"]
    )
    assert result.possible is True
    assert [(s.node, s.id) for s in result.steps] == [
        ("b", "b2"),
        ("a", "a2"),
    ]


def test_fingerprints_are_ignored_as_metadata():
    # None of the artifacts share the same fingerprint value; transitions
    # must still plan purely from the dependency lists.
    nodes = chain_nodes()
    artifacts = [
        BundleArtifact(
            id=a.id,
            node=a.node,
            fingerprint=f"fp-{a.id}",
            size=7,
            dependencies=a.dependencies,
        )
        for a in chain_artifacts()
    ]
    result = plan_release_transition(
        nodes, "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
    )
    assert result.possible is True
    assert len(result.steps) == 5


def test_target_alone():
    nodes = [node("solo"), node("other")]
    artifacts = [
        artifact("solo1", "solo"),
        artifact("solo2", "solo"),
        artifact("other1", "other"),  # outside the closure
    ]
    # The outside artifact makes this request an error: sanity check below
    # instead uses only closure artifacts.
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            nodes, "solo", artifacts, ["solo1"], ["solo2"]
        )
    assert exc.value.code == "artifact_outside_closure"

    result = plan_release_transition(
        nodes,
        "solo",
        [artifact("solo1", "solo"), artifact("solo2", "solo")],
        ["solo1"],
        ["solo2"],
    )
    assert result.possible is True
    assert [(s.node, s.id) for s in result.steps] == [("solo", "solo2")]


def test_eight_node_closure_is_allowed():
    names = [f"n{i}" for i in range(8)]
    nodes = [node(names[0])] + [
        node(names[i], deps=[names[i - 1]]) for i in range(1, 8)
    ]
    artifacts = [
        artifact(f"{n}-1", n, {} if i == 0 else {names[i - 1]: [f"{names[i-1]}-1"]})
        for i, n in enumerate(names)
    ]
    ids = [f"{n}-1" for n in names]
    result = plan_release_transition(nodes, names[-1], artifacts, ids, ids)
    assert result.possible is True
    assert result.steps == []


# ------------------------------------------------------------------ errors


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(chain_nodes(), "ghost", [], ["x"], ["y"])
    assert exc.value.code == "unknown_target"
    assert exc.value.details == {"target": "ghost"}


def test_closure_too_large_error():
    names = [f"n{i}" for i in range(MAX_CLOSURE_NODES + 1)]
    nodes = [node(names[0])] + [
        node(names[i], deps=[names[i - 1]]) for i in range(1, len(names))
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            nodes, names[-1], [], ["x"], ["y"]
        )
    assert exc.value.code == "closure_too_large"
    assert exc.value.details == {
        "target": names[-1],
        "closure_size": MAX_CLOSURE_NODES + 1,
        "limit": MAX_CLOSURE_NODES,
    }


def test_duplicate_artifact_id_error():
    artifacts = [
        artifact("same", "base"),
        artifact("same", "svc"),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(), "web", artifacts, ["x"], ["y"]
        )
    assert exc.value.code == "duplicate_artifact_id"
    assert exc.value.details == {"ids": ["same"]}


def test_artifact_outside_closure_error():
    nodes = [node("base"), node("docs")]
    artifacts = [
        artifact("base1", "base"),
        artifact("docs1", "docs"),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            nodes, "base", artifacts, ["base1"], ["base1"]
        )
    assert exc.value.code == "artifact_outside_closure"
    assert exc.value.details == {"ids": ["docs1"]}


def test_too_few_artifacts_for_node_error():
    # svc and web offer nothing: both are reported, sorted by node name.
    nodes = chain_nodes()
    artifacts = [artifact("b1", "base"), artifact("b2", "base")]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            nodes, "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
        )
    assert exc.value.code == "invalid_artifact_count"
    assert exc.value.details == {
        "nodes": [
            {"node": "svc", "count": 0, "min": 1, "max": 3},
            {"node": "web", "count": 0, "min": 1, "max": 3},
        ]
    }


def test_too_many_artifacts_for_node_error():
    nodes = [node("a")]
    artifacts = [artifact(f"a{i}", "a") for i in range(
        MAX_ARTIFACTS_PER_NODE + 1
    )]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            nodes, "a", artifacts, ["a0"], ["a0"]
        )
    assert exc.value.code == "invalid_artifact_count"
    assert exc.value.details == {
        "nodes": [
            {"node": "a", "count": 4, "min": 1, "max": 3}
        ]
    }


def test_invalid_dependency_map_error():
    artifacts = chain_artifacts()
    # s1 must map base; drop the key.
    bad = next(a for a in artifacts if a.id == "s1").model_copy(deep=True)
    bad.dependencies = {}
    artifacts = [bad if a.id == "s1" else a for a in artifacts]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(), "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
        )
    assert exc.value.code == "invalid_dependency_map"
    assert exc.value.details == {
        "artifacts": [{"id": "s1", "missing": ["base"]}]
    }


def test_unknown_artifact_reference_error():
    artifacts = chain_artifacts()
    bad = next(a for a in artifacts if a.id == "s1").model_copy(deep=True)
    bad.dependencies = {"base": ["ghost"]}
    artifacts = [bad if a.id == "s1" else a for a in artifacts]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(), "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
        )
    assert exc.value.code == "unknown_artifact_reference"
    assert exc.value.details == {"ids": ["ghost"]}


def test_wrong_node_reference_error():
    artifacts = chain_artifacts()
    bad = next(a for a in artifacts if a.id == "s1").model_copy(deep=True)
    bad.dependencies = {"base": ["b1", "w1"]}  # w1 belongs to web
    artifacts = [bad if a.id == "s1" else a for a in artifacts]
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(), "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
        )
    assert exc.value.code == "invalid_artifact_reference"
    assert exc.value.details == {
        "references": [
            {"artifact": "s1", "dependency": "base", "artifact_id": "w1"}
        ]
    }


def test_endpoint_unknown_id_error():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(),
            "web",
            chain_artifacts(),
            ["b1", "s1", "ghost"],
            CHAIN_NEXT,
        )
    assert exc.value.code == "invalid_endpoint_bundle"
    assert "current" in exc.value.details
    assert exc.value.details["current"]["unknown"] == ["ghost"]
    assert "next" not in exc.value.details


def test_endpoint_duplicate_and_missing_node_error():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(),
            "web",
            chain_artifacts(),
            ["b1", "b2"],  # two artifacts for base, none for svc/web
            CHAIN_NEXT,
        )
    assert exc.value.code == "invalid_endpoint_bundle"
    current = exc.value.details["current"]
    # b1 and b2 are distinct ids, so no id is repeated; base is covered
    # twice and svc/web not at all.
    assert "duplicate" not in current
    assert current["missing_nodes"] == ["svc", "web"]
    assert current["multiple_nodes"] == ["base"]


def test_endpoint_same_id_twice_error():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(),
            "web",
            chain_artifacts(),
            ["b1", "b1", "s1", "w1"],
            CHAIN_NEXT,
        )
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details["current"]["duplicate"] == ["b1"]


def test_endpoint_inconsistent_bundle_error():
    # w1 accepts only s1; pairing it with s2 makes the current bundle
    # internally inconsistent even though every node is covered.
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(),
            "web",
            chain_artifacts(),
            ["b2", "s2", "w1"],
            CHAIN_NEXT,
        )
    assert exc.value.code == "invalid_endpoint_bundle"
    inconsistent = exc.value.details["current"]["inconsistent"]
    assert {
        "artifact": "w1",
        "dependency": "svc",
        "selected": "s2",
        "acceptable": ["s1"],
    } in inconsistent


def test_both_endpoints_report_problems_together():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            chain_nodes(),
            "web",
            chain_artifacts(),
            ["b1", "s1", "ghost-cur"],
            ["b2", "s2", "ghost-next"],
        )
    assert exc.value.code == "invalid_endpoint_bundle"
    assert exc.value.details["current"]["unknown"] == ["ghost-cur"]
    assert exc.value.details["next"]["unknown"] == ["ghost-next"]


def test_graph_errors_are_reused():
    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            [node("a", deps=["b"]), node("b", deps=["a"])],
            "a",
            [],
            ["x"],
            ["y"],
        )
    assert exc.value.code == "cycle_detected"

    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            [node("a"), node("a")], "a", [], ["x"], ["y"]
        )
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        plan_release_transition(
            [node("a", deps=["ghost"])], "a", [], ["x"], ["y"]
        )
    assert exc.value.code == "missing_dependency"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    nodes = chain_nodes()
    artifacts = chain_artifacts()
    baseline = plan_release_transition(
        nodes, "web", artifacts, CHAIN_CURRENT, CHAIN_NEXT
    ).model_dump()
    rng = random.Random(17)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        shuffled_artifacts = artifacts[:]
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_artifacts)
        result = plan_release_transition(
            shuffled_nodes,
            "web",
            shuffled_artifacts,
            list(reversed(CHAIN_CURRENT)),
            list(reversed(CHAIN_NEXT)),
        ).model_dump()
        assert result == baseline


# --------------------------------------------------------------------- HTTP


def _payload(
    nodes: list[Node],
    target: str,
    artifacts: list[BundleArtifact],
    current: list[str],
    next_: list[str],
) -> dict:
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
        "current_ids": current,
        "next_ids": next_,
    }


def test_http_transition_possible():
    resp = client.post(
        "/release-transitions",
        json=_payload(
            chain_nodes(),
            "web",
            chain_artifacts(),
            CHAIN_CURRENT,
            CHAIN_NEXT,
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["possible"] is True
    assert [
        (step["node"], step["id"]) for step in body["steps"]
    ] == [
        ("web", "w3"),
        ("svc", "s3"),
        ("base", "b2"),
        ("svc", "s2"),
        ("web", "w2"),
    ]
    assert body["steps"][0]["bundle"] == {
        "base": "b1",
        "svc": "s1",
        "web": "w3",
    }
    assert body["steps"][-1]["bundle"] == {
        "base": "b2",
        "svc": "s2",
        "web": "w2",
    }


def test_http_transition_same_bundle():
    resp = client.post(
        "/release-transitions",
        json=_payload(
            chain_nodes(),
            "web",
            chain_artifacts(),
            CHAIN_CURRENT,
            CHAIN_CURRENT,
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"possible": True, "steps": []}


def test_http_transition_impossible():
    nodes = [node("a"), node("b", deps=["a"])]
    artifacts = [
        artifact("a1", "a"),
        artifact("a2", "a"),
        artifact("b1", "b", {"a": ["a1"]}),
        artifact("b2", "b", {"a": ["a2"]}),
    ]
    resp = client.post(
        "/release-transitions",
        json=_payload(
            nodes, "b", artifacts, ["a1", "b1"], ["a2", "b2"]
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"possible": False, "steps": []}


def test_http_unknown_target_is_400():
    resp = client.post(
        "/release-transitions",
        json=_payload(
            chain_nodes(),
            "ghost",
            chain_artifacts(),
            CHAIN_CURRENT,
            CHAIN_NEXT,
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_closure_too_large_is_400():
    names = [f"n{i}" for i in range(MAX_CLOSURE_NODES + 1)]
    nodes = [node(names[0])] + [
        node(names[i], deps=[names[i - 1]]) for i in range(1, len(names))
    ]
    resp = client.post(
        "/release-transitions",
        json=_payload(nodes, names[-1], [], ["x"], ["y"]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "closure_too_large"


def test_http_invalid_endpoint_is_400():
    payload = _payload(
        chain_nodes(),
        "web",
        chain_artifacts(),
        ["b1", "s1", "ghost"],
        CHAIN_NEXT,
    )
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_endpoint_bundle"


def test_http_outside_closure_artifact_is_400():
    nodes = [*chain_nodes(), node("docs")]
    payload = _payload(
        nodes,
        "web",
        [*chain_artifacts(), artifact("docs1", "docs")],
        CHAIN_CURRENT,
        CHAIN_NEXT,
    )
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "artifact_outside_closure"


def test_http_empty_endpoint_list_is_422():
    payload = _payload(
        chain_nodes(), "web", chain_artifacts(), [], CHAIN_NEXT
    )
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422


def test_http_empty_endpoint_id_is_422():
    payload = _payload(
        chain_nodes(),
        "web",
        chain_artifacts(),
        ["b1", "s1", ""],
        CHAIN_NEXT,
    )
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    payload = _payload(
        chain_nodes(),
        "web",
        chain_artifacts(),
        CHAIN_CURRENT,
        CHAIN_NEXT,
    )
    payload["bogus"] = 1
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422


def test_http_missing_section_rejected():
    payload = _payload(
        chain_nodes(),
        "web",
        chain_artifacts(),
        CHAIN_CURRENT,
        CHAIN_NEXT,
    )
    del payload["next_ids"]
    resp = client.post("/release-transitions", json=payload)
    assert resp.status_code == 422
