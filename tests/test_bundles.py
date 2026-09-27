"""Tests for the /release-bundles endpoint and its planning core."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.bundles import MAX_ARTIFACTS, plan_release_bundle
from app.main import app
from app.models import BundleArtifact, Node
from app.planner import PlanError, compute_fingerprints, validate_graph

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def fingerprints_of(nodes: list[Node]) -> dict[str, str]:
    graph, contents, order = validate_graph(nodes)
    return compute_fingerprints(graph, contents, order)


def bundle_artifact(
    id_: str,
    node_name: str,
    size: int,
    dependencies: dict[str, list[str]] | None = None,
    fingerprint: str | None = None,
    fps: dict[str, str] | None = None,
) -> BundleArtifact:
    if fingerprint is None:
        assert fps is not None
        fingerprint = fps[node_name]
    return BundleArtifact(
        id=id_,
        node=node_name,
        fingerprint=fingerprint,
        size=size,
        dependencies=dependencies or {},
    )


# The README example graph: core <- util <- api <- web, plus standalone docs.
def readme_nodes() -> list[Node]:
    return [
        node("core", "core-src-v1"),
        node("util", "util-src-v1", ["core"]),
        node("api", "api-src-v1", ["util", "core"]),
        node("web", "web-src-v1", ["api"]),
        node("docs", "docs-src-v1"),
    ]


# ------------------------------------------------------------- feasibility


def test_single_artifact_per_chain_node():
    nodes = [node("a", "a"), node("b", "b", ["a"]), node("c", "c", ["b"])]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("a1", "a", 4, fps=fps),
        bundle_artifact("b1", "b", 6, {"a": ["a1"]}, fps=fps),
        bundle_artifact("c1", "c", 5, {"b": ["b1"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "c", artifacts)
    assert result.ready is True
    assert result.artifact_ids == ["a1", "b1", "c1"]
    assert result.total_size == 15


def test_bundle_covers_only_target_closure():
    # docs is unrelated to web and must not be part of the bundle even when
    # a matching artifact for it is supplied.
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 4, fps=fps),
        bundle_artifact("util1", "util", 6, {"core": ["core1"]}, fps=fps),
        bundle_artifact(
            "api1", "api", 10, {"core": ["core1"], "util": ["util1"]}, fps=fps
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api1"]}, fps=fps),
        bundle_artifact("docs1", "docs", 9, fps=fps),  # outside closure
    ]
    result = plan_release_bundle(nodes, "web", artifacts)
    assert result.ready is True
    assert result.artifact_ids == ["api1", "core1", "util1", "web1"]
    assert result.total_size == 25


def test_target_alone_with_no_dependencies():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [bundle_artifact("docs1", "docs", 3, fps=fps)]
    result = plan_release_bundle(nodes, "docs", artifacts)
    assert result.ready is True
    assert result.artifact_ids == ["docs1"]
    assert result.total_size == 3


def test_shared_dependency_counted_once():
    # Diamond: app -> x, y -> base; base appears once in size and ids.
    nodes = [
        node("base", "b0"),
        node("x", "x", ["base"]),
        node("y", "y", ["base"]),
        node("app", "a", ["x", "y"]),
    ]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("base1", "base", 3, fps=fps),
        bundle_artifact("x1", "x", 2, {"base": ["base1"]}, fps=fps),
        bundle_artifact("y1", "y", 2, {"base": ["base1"]}, fps=fps),
        bundle_artifact(
            "app1", "app", 1, {"x": ["x1"], "y": ["y1"]}, fps=fps
        ),
    ]
    result = plan_release_bundle(nodes, "app", artifacts)
    assert result.ready is True
    assert result.artifact_ids == ["app1", "base1", "x1", "y1"]
    assert result.total_size == 8


# ------------------------------------------------------- exact optimum


def test_picks_minimum_total_size():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core-big", "core", 4, fps=fps),
        bundle_artifact("core-small", "core", 2, fps=fps),
        bundle_artifact("util-big", "util", 6, {"core": ["core-big"]}, fps=fps),
        bundle_artifact("util-small", "util", 3, {"core": ["core-small"]}, fps=fps),
        bundle_artifact(
            "api-big",
            "api",
            10,
            {"core": ["core-big"], "util": ["util-big"]},
            fps=fps,
        ),
        bundle_artifact(
            "api-small",
            "api",
            7,
            {"core": ["core-small"], "util": ["util-small"]},
            fps=fps,
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api-big", "api-small"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "web", artifacts)
    assert result.ready is True
    # core-small + util-small + api-small + web1 = 17; big chain would be 25.
    assert result.artifact_ids == ["api-small", "core-small", "util-small", "web1"]
    assert result.total_size == 17


def test_more_expensive_artifact_may_be_required_by_dependency_lists():
    # The small web artifact only accepts the big (consistent) api chain.
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core-big", "core", 4, fps=fps),
        bundle_artifact("core-small", "core", 2, fps=fps),
        bundle_artifact("util-big", "util", 6, {"core": ["core-big"]}, fps=fps),
        bundle_artifact("util-small", "util", 3, {"core": ["core-small"]}, fps=fps),
        bundle_artifact(
            "api-big",
            "api",
            10,
            {"core": ["core-big"], "util": ["util-big"]},
            fps=fps,
        ),
        bundle_artifact(
            "api-small",
            "api",
            7,
            {"core": ["core-small"], "util": ["util-small"]},
            fps=fps,
        ),
        bundle_artifact(
            "web-cheap", "web", 1, {"api": ["api-big"]}, fps=fps
        ),  # forces big chain
        bundle_artifact(
            "web-dear", "web", 9, {"api": ["api-small"]}, fps=fps
        ),  # cheap chain + dear web
    ]
    result = plan_release_bundle(nodes, "web", artifacts)
    assert result.ready is True
    # big chain + cheap web = 4+6+10+1 = 21; small chain + dear web = 2+3+7+9 = 21 too.
    # Tie: compare sorted id lists; the big-chain bundle is lexicographically smaller.
    assert result.total_size == 21
    assert result.artifact_ids == ["api-big", "core-big", "util-big", "web-cheap"]


def test_tie_break_is_sorted_id_list_lexicographic():
    # Two equally sized bundles differ only in their base artifact id; the
    # lexicographically smaller sorted id list must win.
    nodes = [node("base", "b"), node("app", "a", ["base"])]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("zzz-base", "base", 3, fps=fps),
        bundle_artifact("aaa-base", "base", 3, fps=fps),
        bundle_artifact("app-z", "app", 1, {"base": ["zzz-base"]}, fps=fps),
        bundle_artifact("app-a", "app", 1, {"base": ["aaa-base"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "app", artifacts)
    assert result.ready is True
    assert result.artifact_ids == ["aaa-base", "app-a"]
    assert result.total_size == 4


def test_list_prefix_semantics_in_lexicographic_tie():
    # Bundles have equal closure cardinality; ["a", "b"] < ["a", "c"].
    nodes = [node("b", "b"), node("a", "a", ["b"])]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("b1", "b", 2, fps=fps),
        bundle_artifact("b2", "b", 2, fps=fps),
        bundle_artifact("a1", "a", 2, {"b": ["b2"]}, fps=fps),
        bundle_artifact("a0", "a", 2, {"b": ["b1"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "a", artifacts)
    assert result.ready is True
    # ["a0","b1"] vs ["a1","b2"]: "a0" < "a1".
    assert result.artifact_ids == ["a0", "b1"]


# ------------------------------------------------------------ infeasibility


def test_stale_fingerprint_artifact_is_not_selectable():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 4, fingerprint="0" * 64),
        bundle_artifact("util1", "util", 6, {"core": ["core1"]}, fps=fps),
        bundle_artifact(
            "api1", "api", 10, {"core": ["core1"], "util": ["util1"]}, fps=fps
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api1"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "web", artifacts)
    assert result.ready is False
    assert result.artifact_ids == []
    assert result.total_size == 0


def test_missing_artifact_for_closure_node_is_infeasible():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 4, fps=fps),
        bundle_artifact("util1", "util", 6, {"core": ["core1"]}, fps=fps),
        # The only api artifact is structurally valid but has a stale
        # fingerprint, so the closure cannot be covered: not ready (not 400).
        bundle_artifact(
            "api-old",
            "api",
            1,
            {"core": ["core1"], "util": ["util1"]},
            fingerprint="1" * 64,
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api-old"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "web", artifacts)
    assert result.ready is False
    assert result.artifact_ids == []
    assert result.total_size == 0


def test_no_artifacts_at_all_is_not_ready():
    result = plan_release_bundle(readme_nodes(), "web", [])
    assert result.ready is False
    assert result.artifact_ids == []
    assert result.total_size == 0


def test_two_artifacts_for_same_node_cannot_both_be_chosen():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("a1", "a", 1, fps=fps),
        bundle_artifact("a2", "a", 1, fps=fps),
        bundle_artifact("b1", "b", 1, {"a": ["a1"]}, fps=fps),
        bundle_artifact("b2", "b", 1, {"a": ["a2"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "b", artifacts)
    # Two equally small bundles: ["a1","b1"] wins lexicographically; each
    # contains exactly one artifact per node.
    assert result.ready is True
    assert result.artifact_ids == ["a1", "b1"]


def test_dependency_id_not_accepted_makes_bundle_infeasible():
    # b1 accepts a2 only; a1 exists with matching fingerprint but is not
    # acceptable to b1, and a2 is stale -> no feasible bundle.
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("a1", "a", 1, fps=fps),
        bundle_artifact("a2", "a", 1, fingerprint="f" * 64),
        bundle_artifact("b1", "b", 1, {"a": ["a2"]}, fps=fps),
    ]
    result = plan_release_bundle(nodes, "b", artifacts)
    assert result.ready is False
    assert result.artifact_ids == []
    assert result.total_size == 0


def test_stale_upstream_breaks_shared_downstream_bundle():
    # Diamond; x and y demand different base artifacts, only one base is
    # fresh, so no single base id can satisfy both arms.
    nodes = [
        node("base", "b0"),
        node("x", "x", ["base"]),
        node("y", "y", ["base"]),
        node("app", "a", ["x", "y"]),
    ]
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("base1", "base", 3, fps=fps),
        bundle_artifact("base2", "base", 1, fingerprint="9" * 64),
        bundle_artifact("x1", "x", 2, {"base": ["base1"]}, fps=fps),
        bundle_artifact("y1", "y", 2, {"base": ["base2"]}, fps=fps),
        bundle_artifact(
            "app1", "app", 1, {"x": ["x1"], "y": ["y1"]}, fps=fps
        ),
    ]
    result = plan_release_bundle(nodes, "app", artifacts)
    assert result.ready is False
    assert result.artifact_ids == []
    assert result.total_size == 0


# ------------------------------------------------------------------ errors


def test_duplicate_artifact_id_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("same", "core", 1, fps=fps),
        bundle_artifact("same", "util", 1, {"core": ["same"]}, fps=fps),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "web", artifacts)
    assert exc.value.code == "duplicate_artifact_id"
    assert exc.value.details == {"ids": ["same"]}


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(readme_nodes(), "ghost", [])
    assert exc.value.code == "unknown_target"
    assert exc.value.details == {"target": "ghost"}


def test_unknown_artifact_node_error():
    nodes = readme_nodes()
    artifacts = [
        BundleArtifact(
            id="x", node="ghost", fingerprint="f" * 64, size=1, dependencies={}
        )
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "web", artifacts)
    assert exc.value.code == "unknown_artifact_node"
    assert exc.value.details == {"names": ["ghost"]}


def test_dependency_map_missing_key_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    # util directly depends on core but omits it.
    artifacts = [bundle_artifact("u1", "util", 1, {}, fps=fps)]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "util", artifacts)
    assert exc.value.code == "invalid_dependency_map"
    assert exc.value.details == {
        "artifacts": [{"id": "u1", "missing": ["core"]}]
    }


def test_dependency_map_extra_key_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    # core has no dependencies but maps util.
    artifacts = [bundle_artifact("c1", "core", 1, {"util": ["u1"]}, fps=fps)]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "core", artifacts)
    assert exc.value.code == "invalid_dependency_map"
    assert exc.value.details == {
        "artifacts": [{"id": "c1", "unexpected": ["util"]}]
    }


def test_dependency_map_reports_multiple_bad_artifacts_sorted_by_id():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    # model_construct bypasses the field validator (empty acceptable lists
    # are a 422 at the HTTP layer); this checks the core's key-set validation
    # and its id-sorted reporting.
    artifacts = [
        BundleArtifact.model_construct(
            id="z1", node="core", fingerprint=fps["core"], size=1,
            dependencies={"api": []},
        ),
        BundleArtifact.model_construct(
            id="a1", node="core", fingerprint=fps["core"], size=1,
            dependencies={"util": []},
        ),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "core", artifacts)
    assert exc.value.code == "invalid_dependency_map"
    assert [entry["id"] for entry in exc.value.details["artifacts"]] == ["a1", "z1"]


def test_unknown_artifact_reference_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 1, fps=fps),
        bundle_artifact("util1", "util", 1, {"core": ["ghost-id"]}, fps=fps),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "util", artifacts)
    assert exc.value.code == "unknown_artifact_reference"
    assert exc.value.details == {"ids": ["ghost-id"]}


def test_reference_to_artifact_of_wrong_node_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 1, fps=fps),
        bundle_artifact("web1", "web", 1, {"api": ["api1"]}, fps=fps),
        bundle_artifact(
            "util1", "util", 1, {"core": ["core1", "web1"]}, fps=fps
        ),  # web1 belongs to web, not core
        bundle_artifact(
            "api1",
            "api",
            1,
            {"core": ["core1"], "util": ["util1"]},
            fps=fps,
        ),
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "web", artifacts)
    assert exc.value.code == "invalid_artifact_reference"
    assert exc.value.details == {
        "references": [
            {"artifact": "util1", "dependency": "core", "artifact_id": "web1"}
        ]
    }


def test_too_many_artifacts_error():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact(f"a{i:02d}", "docs", 1, fps=fps)
        for i in range(MAX_ARTIFACTS + 1)
    ]
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(nodes, "docs", artifacts)
    assert exc.value.code == "too_many_artifacts"
    assert exc.value.details == {"artifacts": MAX_ARTIFACTS + 1, "limit": MAX_ARTIFACTS}


def test_graph_errors_are_reused():
    fps = {"a": "x", "b": "y"}
    with pytest.raises(PlanError) as exc:
        plan_release_bundle(
            [node("a", "a", ["b"]), node("b", "b", ["a"])], "a", []
        )
    assert exc.value.code == "cycle_detected"

    with pytest.raises(PlanError) as exc:
        plan_release_bundle([node("a", "1"), node("a", "2")], "a", [])
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        plan_release_bundle([node("a", "a", ["ghost"])], "a", [])
    assert exc.value.code == "missing_dependency"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core-big", "core", 4, fps=fps),
        bundle_artifact("core-small", "core", 2, fps=fps),
        bundle_artifact("util-big", "util", 6, {"core": ["core-big"]}, fps=fps),
        bundle_artifact("util-small", "util", 3, {"core": ["core-small"]}, fps=fps),
        bundle_artifact(
            "api-big",
            "api",
            10,
            {"core": ["core-big"], "util": ["util-big"]},
            fps=fps,
        ),
        bundle_artifact(
            "api-small",
            "api",
            7,
            {"core": ["core-small"], "util": ["util-small"]},
            fps=fps,
        ),
        bundle_artifact(
            "web1", "web", 5, {"api": ["api-big", "api-small"]}, fps=fps
        ),
        bundle_artifact("docs1", "docs", 2, fps=fps),
    ]
    baseline = plan_release_bundle(nodes, "web", artifacts).model_dump()
    rng = random.Random(71)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        shuffled_artifacts = artifacts[:]
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_artifacts)
        result = plan_release_bundle(
            shuffled_nodes, "web", shuffled_artifacts
        ).model_dump()
        assert result == baseline


def test_dependency_list_inner_order_does_not_matter():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    ordered = [
        bundle_artifact("core1", "core", 4, fps=fps),
        bundle_artifact("util1", "util", 6, {"core": ["core1"]}, fps=fps),
        bundle_artifact(
            "api1", "api", 10, {"core": ["core1"], "util": ["util1"]}, fps=fps
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api1"]}, fps=fps),
    ]
    shuffled = [
        bundle_artifact("core1", "core", 4, fps=fps),
        bundle_artifact("util1", "util", 6, {"core": ["core1"]}, fps=fps),
        bundle_artifact(
            "api1", "api", 10, {"util": ["util1"], "core": ["core1"]}, fps=fps
        ),
        bundle_artifact("web1", "web", 5, {"api": ["api1"]}, fps=fps),
    ]
    assert plan_release_bundle(nodes, "web", ordered).model_dump() == (
        plan_release_bundle(nodes, "web", shuffled).model_dump()
    )


# --------------------------------------------------------------------- HTTP


def _payload(nodes, target, artifacts):
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
    }


def _readme_payload():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core-big", "core", 4, fps=fps),
        bundle_artifact("core-small", "core", 2, fps=fps),
        bundle_artifact("util-big", "util", 6, {"core": ["core-big"]}, fps=fps),
        bundle_artifact("util-small", "util", 3, {"core": ["core-small"]}, fps=fps),
        bundle_artifact(
            "api-big",
            "api",
            10,
            {"core": ["core-big"], "util": ["util-big"]},
            fps=fps,
        ),
        bundle_artifact(
            "api-small",
            "api",
            7,
            {"core": ["core-small"], "util": ["util-small"]},
            fps=fps,
        ),
        bundle_artifact(
            "web1", "web", 5, {"api": ["api-big", "api-small"]}, fps=fps
        ),
    ]
    return _payload(nodes, "web", artifacts)


def test_http_release_bundle_ready():
    resp = client.post("/release-bundles", json=_readme_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "ready": True,
        "artifact_ids": ["api-small", "core-small", "util-small", "web1"],
        "total_size": 17,
    }


def test_http_release_bundle_not_ready():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact("core1", "core", 1, fingerprint="0" * 64),
        bundle_artifact("util1", "util", 1, {"core": ["core1"]}, fps=fps),
        bundle_artifact(
            "api1", "api", 1, {"core": ["core1"], "util": ["util1"]}, fps=fps
        ),
        bundle_artifact("web1", "web", 1, {"api": ["api1"]}, fps=fps),
    ]
    resp = client.post("/release-bundles", json=_payload(nodes, "web", artifacts))
    assert resp.status_code == 200
    assert resp.json() == {"ready": False, "artifact_ids": [], "total_size": 0}


def test_http_unknown_target_is_400():
    payload = _readme_payload()
    payload["target"] = "ghost"
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_duplicate_artifact_id_is_400():
    payload = _readme_payload()
    payload["artifacts"][0]["id"] = "web1"
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_artifact_id"


def test_http_unknown_artifact_node_is_400():
    payload = _readme_payload()
    payload["artifacts"][0]["node"] = "ghost"
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_artifact_node"


def test_http_invalid_dependency_map_is_400():
    payload = _readme_payload()
    # util-big maps core but drop the key.
    util_big = next(a for a in payload["artifacts"] if a["id"] == "util-big")
    del util_big["dependencies"]["core"]
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_dependency_map"


def test_http_unknown_artifact_reference_is_400():
    payload = _readme_payload()
    payload["artifacts"][2]["dependencies"]["core"] = ["nope"]
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_artifact_reference"


def test_http_wrong_node_reference_is_400():
    payload = _readme_payload()
    # util-big lists web1 (a web artifact) as acceptable for core.
    payload["artifacts"][2]["dependencies"]["core"] = ["core-big", "web1"]
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_artifact_reference"


def test_http_too_many_artifacts_is_400():
    nodes = readme_nodes()
    fps = fingerprints_of(nodes)
    artifacts = [
        bundle_artifact(f"a{i:02d}", "docs", 1, fps=fps)
        for i in range(MAX_ARTIFACTS + 1)
    ]
    resp = client.post("/release-bundles", json=_payload(nodes, "docs", artifacts))
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "too_many_artifacts"
    assert body["details"] == {"artifacts": 17, "limit": 16}


def test_http_cycle_is_400():
    payload = _readme_payload()
    payload["nodes"] = [
        {"name": "a", "content_digest": "a", "dependency_names": ["b"]},
        {"name": "b", "content_digest": "b", "dependency_names": ["a"]},
    ]
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["artifacts"][0].__setitem__("size", 0),
        lambda p: p["artifacts"][0].__setitem__("size", -3),
        lambda p: p["artifacts"][0].__setitem__("id", ""),
        lambda p: p["artifacts"][0].__setitem__("node", ""),
        lambda p: p["artifacts"][0].__setitem__("fingerprint", ""),
    ],
)
def test_http_invalid_artifact_fields_are_422(mutate):
    payload = _readme_payload()
    mutate(payload)
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 422


def test_http_empty_dependency_acceptable_list_is_422():
    payload = _readme_payload()
    payload["artifacts"][2]["dependencies"]["core"] = []
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    payload = _readme_payload()
    payload["bogus"] = 1
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 422


def test_http_missing_section_rejected():
    payload = _readme_payload()
    del payload["target"]
    resp = client.post("/release-bundles", json=payload)
    assert resp.status_code == 422
