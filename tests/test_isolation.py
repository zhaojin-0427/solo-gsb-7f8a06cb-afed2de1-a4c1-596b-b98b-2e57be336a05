"""Tests for the /failure-isolation endpoint and its planning core."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.isolation import MAX_CANDIDATES, plan_failure_isolation
from app.main import app
from app.models import Node
from app.planner import PlanError

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def plan(nodes, failed, targets, costs):
    return plan_failure_isolation(nodes, failed, targets, costs)


# ------------------------------------------------------------- basic cuts


def test_single_path_cheapest_node():
    # core -> util -> api -> web (deps point web back at core).
    nodes = [
        node("core", "c"),
        node("util", "u", ["core"]),
        node("api", "a", ["util"]),
        node("web", "w", ["api"]),
    ]
    result = plan(nodes, ["core"], ["web"], {"util": 1, "api": 5})
    assert result.isolated == ["util"]
    assert result.total_cost == 1


def test_must_cut_every_parallel_path():
    # Diamond: base -> b, c -> m -> t; cutting one arm is not enough, and
    # the merge node m is too expensive.
    nodes = [
        node("base", "b0"),
        node("b", "b1", ["base"]),
        node("c", "c1", ["base"]),
        node("m", "m", ["b", "c"]),
        node("t", "t", ["m"]),
    ]
    result = plan(nodes, ["base"], ["t"], {"b": 1, "c": 1, "m": 20})
    assert result.isolated == ["b", "c"]
    assert result.total_cost == 2


def test_common_downstream_node_may_be_cheaper():
    nodes = [
        node("base", "b0"),
        node("b", "b1", ["base"]),
        node("c", "c1", ["base"]),
        node("m", "m", ["b", "c"]),
        node("t", "t", ["m"]),
    ]
    result = plan(nodes, ["base"], ["t"], {"b": 8, "c": 8, "m": 5})
    assert result.isolated == ["m"]
    assert result.total_cost == 5


def test_isolating_one_arm_is_infeasible_even_if_cheap():
    nodes = [
        node("base", "b0"),
        node("b", "b1", ["base"]),
        node("c", "c1", ["base"]),
        node("m", "m", ["b", "c"]),
        node("t", "t", ["m"]),
    ]
    # b alone leaves base -> c -> m -> t open, so the optimum is m despite
    # b being almost free.
    result = plan(nodes, ["base"], ["t"], {"b": 1, "c": 100, "m": 10})
    assert result.isolated == ["m"]
    assert result.total_cost == 10


def test_multiple_failed_nodes_and_targets():
    # f1 -> a -> t1 and f2 -> b -> t2: both chains must be severed.
    nodes = [
        node("f1", "f"),
        node("f2", "f"),
        node("a", "a", ["f1"]),
        node("b", "b", ["f2"]),
        node("t1", "t", ["a"]),
        node("t2", "t", ["b"]),
    ]
    result = plan(
        nodes, ["f1", "f2"], ["t1", "t2"], {"a": 5, "b": 5}
    )
    assert result.isolated == ["a", "b"]
    assert result.total_cost == 10


def test_common_cut_serves_all_failures_and_targets():
    # f1 -> m, f2 -> m -> t1, m -> t2.
    nodes = [
        node("f1", "f"),
        node("f2", "f"),
        node("m", "m", ["f1", "f2"]),
        node("t1", "t", ["m"]),
        node("t2", "t", ["m"]),
    ]
    result = plan(
        nodes, ["f2", "f1"], ["t2", "t1"], {"m": 3}
    )
    assert result.isolated == ["m"]
    assert result.total_cost == 3


def test_path_may_pass_through_another_nonfailed_candidate():
    # f1 -> f2 -> a -> t; only f1 is failed: isolating f2 or a cuts it.
    nodes = [
        node("f1", "f"),
        node("f2", "f", ["f1"]),
        node("a", "a", ["f2"]),
        node("t", "t", ["a"]),
    ]
    result = plan(nodes, ["f1"], ["t"], {"f2": 2, "a": 1})
    assert result.isolated == ["a"]
    assert result.total_cost == 1


def test_all_failed_sources_are_seed_points():
    # f1 -> f2 -> a -> t with BOTH f1, f2 failed: isolating a still works,
    # because the reachability walk starts from every failed node.
    nodes = [
        node("f1", "f"),
        node("f2", "f", ["f1"]),
        node("a", "a", ["f2"]),
        node("t", "t", ["a"]),
    ]
    result = plan(nodes, ["f1", "f2"], ["t"], {"a": 1})
    assert result.isolated == ["a"]
    assert result.total_cost == 1


# ----------------------------------------------------------- tie breaking


def test_tie_break_prefers_lexicographically_smaller_name():
    # Two disjoint chains base -> a -> x -> app and base -> b -> y -> app;
    # every candidate costs 1, so any one-node cut per arm totals 2.  The
    # lexicographically smallest sorted list among {a,b}, {a,y}, {b,x},
    # {x,y} is ["a", "b"].
    nodes = [
        node("base", "0"),
        node("a", "a", ["base"]),
        node("b", "b", ["base"]),
        node("x", "x", ["a"]),
        node("y", "y", ["b"]),
        node("app", "p", ["x", "y"]),
    ]
    result = plan(nodes, ["base"], ["app"], {"a": 1, "b": 1, "x": 1, "y": 1})
    assert result.isolated == ["a", "b"]
    assert result.total_cost == 2


def test_tie_break_compares_lists_elementwise_not_prefix_only():
    # Paths: base -> a -> c -> t and base -> b -> c -> t.
    # {c} costs 3; {a, b} costs 1 + 2 = 3.  Sorted lists ["c"] vs
    # ["a", "b"]: element-wise "a" < "c", so the two-node set wins even
    # though the one-node list is shorter.
    nodes = [
        node("base", "0"),
        node("a", "a", ["base"]),
        node("b", "b", ["base"]),
        node("c", "c", ["a", "b"]),
        node("t", "t", ["c"]),
    ]
    result = plan(nodes, ["base"], ["t"], {"a": 1, "b": 2, "c": 3})
    assert result.isolated == ["a", "b"]
    assert result.total_cost == 3


def test_zero_cost_unrelated_candidate_is_not_included():
    # z sits on no failed -> target path; {a} vs {a, z} tie at cost 1, and
    # ["a"] < ["a", "z"], so z is dropped.
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
        node("z", "z"),
    ]
    result = plan(nodes, ["f"], ["t"], {"a": 1, "z": 0})
    assert result.isolated == ["a"]
    assert result.total_cost == 1


def test_all_zero_costs_pick_lexicographically_smallest_feasible_set():
    # base -> b, c -> m -> t, everything free: {m}, {b, c}, {b, m}, ... all
    # cost 0; ["b", "c"] is the lexicographically smallest sorted list.
    nodes = [
        node("base", "0"),
        node("b", "b", ["base"]),
        node("c", "c", ["base"]),
        node("m", "m", ["b", "c"]),
        node("t", "t", ["m"]),
    ]
    result = plan(nodes, ["base"], ["t"], {"b": 0, "c": 0, "m": 0})
    assert result.isolated == ["b", "c"]
    assert result.total_cost == 0


# ----------------------------------------------------------- empty result


def test_no_path_returns_empty_set():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t"),  # t depends on nothing failed
    ]
    result = plan(nodes, ["f"], ["t"], {"a": 0})
    assert result.isolated == []
    assert result.total_cost == 0


def test_no_path_when_failure_is_downstream_of_target():
    # t -> a -> f: the failure cannot propagate "up" the dependency edges.
    nodes = [
        node("t", "t"),
        node("a", "a", ["t"]),
        node("f", "f", ["a"]),
    ]
    result = plan(nodes, ["f"], ["t"], {"a": 1})
    assert result.isolated == []
    assert result.total_cost == 0


def test_empty_graph_components_are_fine():
    nodes = [node("f", "f"), node("t", "t"), node("x", "x")]
    result = plan(nodes, ["f"], ["t"], {})
    assert result.isolated == []
    assert result.total_cost == 0


# ------------------------------------------------------------- infeasible


def test_direct_dependency_edge_cannot_be_cut():
    nodes = [node("f", "f"), node("t", "t", ["f"])]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {})
    assert exc.value.code == "isolation_impossible"
    assert exc.value.details["failed_nodes"] == ["f"]
    assert exc.value.details["targets"] == ["t"]


def test_one_uncoverable_path_makes_isolation_impossible():
    # base -> a -> t is cuttable at a, but base -> t directly is not.
    nodes = [
        node("base", "0"),
        node("a", "a", ["base"]),
        node("t", "t", ["a", "base"]),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["base"], ["t"], {"a": 1})
    assert exc.value.code == "isolation_impossible"


def test_unrelated_candidates_do_not_help():
    nodes = [
        node("f", "f"),
        node("t", "t", ["f"]),
        node("z", "z"),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {"z": 0})
    assert exc.value.code == "isolation_impossible"


def test_one_unreachable_cut_is_not_enough():
    # b can be removed but the base -> c -> app arm stays open.
    nodes = [
        node("base", "0"),
        node("b", "b", ["base"]),
        node("c", "c", ["base"]),
        node("app", "a", ["b", "c"]),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["base"], ["app"], {"b": 1})
    assert exc.value.code == "isolation_impossible"


# --------------------------------------------------------------- errors


def test_duplicate_failed_node_is_rejected_first():
    # a is simultaneously a duplicated failure and the target: the
    # duplicate failure must be reported deterministically first.
    with pytest.raises(PlanError) as exc:
        plan([node("a", "a")], ["a", "a"], ["a"], {})
    assert exc.value.code == "duplicate_failed_node"


def test_duplicate_target_error():
    nodes = [node("f", "f"), node("t", "t")]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t", "t"], {})
    assert exc.value.code == "duplicate_target"
    assert exc.value.details["names"] == ["t"]


def test_unknown_failed_node_error():
    with pytest.raises(PlanError) as exc:
        plan([node("t", "t")], ["ghost"], ["t"], {})
    assert exc.value.code == "unknown_failed_node"
    assert exc.value.details["names"] == ["ghost"]


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        plan([node("f", "f")], ["f"], ["ghost"], {})
    assert exc.value.code == "unknown_target"
    assert exc.value.details["names"] == ["ghost"]


def test_unknown_candidate_error():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {"a": 1, "ghost": 2})
    assert exc.value.code == "unknown_candidate"
    assert exc.value.details["names"] == ["ghost"]


def test_failed_target_overlap_error():
    with pytest.raises(PlanError) as exc:
        plan([node("a", "a")], ["a"], ["a"], {})
    assert exc.value.code == "failed_target_overlap"
    assert exc.value.details["names"] == ["a"]


def test_candidate_that_is_failed_node_error():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {"a": 1, "f": 2})
    assert exc.value.code == "invalid_candidate"
    assert exc.value.details["names"] == ["f"]


def test_candidate_that_is_target_error():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {"a": 1, "t": 2})
    assert exc.value.code == "invalid_candidate"
    assert exc.value.details["names"] == ["t"]


def test_too_many_candidates_error():
    names = [f"n{i:02d}" for i in range(MAX_CANDIDATES + 1)]
    nodes = [node("f", "f"), node("t", "t")] + [
        node(name, name) for name in names
    ]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["f"], ["t"], {name: 1 for name in names})
    assert exc.value.code == "too_many_candidates"
    assert exc.value.details["candidates"] == MAX_CANDIDATES + 1
    assert exc.value.details["limit"] == MAX_CANDIDATES


def test_exactly_sixteen_candidates_is_allowed():
    names = [f"n{i:02d}" for i in range(MAX_CANDIDATES)]
    nodes = [node("f", "f"), node("t", "t")] + [
        node(name, name) for name in names
    ]
    result = plan(nodes, ["f"], ["t"], {name: 1 for name in names})
    # No failed -> target path: empty set, regardless of candidates.
    assert result.isolated == []
    assert result.total_cost == 0


def test_graph_errors_are_reused():
    with pytest.raises(PlanError) as exc:
        plan([node("a", "1"), node("a", "2")], ["a"], ["a"], {})
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        plan([node("a", "a", ["ghost"])], ["a"], ["a"], {})
    assert exc.value.code == "missing_dependency"

    nodes = [node("a", "a", ["b"]), node("b", "b", ["a"])]
    with pytest.raises(PlanError) as exc:
        plan(nodes, ["a"], ["b"], {})
    assert exc.value.code == "cycle_detected"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    nodes = [
        node("base", "0"),
        node("b", "b", ["base"]),
        node("c", "c", ["base"]),
        node("m", "m", ["b", "c"]),
        node("app", "a", ["m"]),
        node("extra", "e"),
    ]
    failed = ["base"]
    targets = ["app"]
    costs = {"b": 3, "c": 4, "m": 5, "extra": 0}
    baseline = plan(nodes, failed, targets, costs).model_dump()
    # {m} and {extra, m} tie at cost 5; ["extra", "m"] < ["m"] element-wise,
    # so the zero-cost unrelated candidate is pulled in (same rule as
    # /cache-retention's kept-list tie-break).
    assert baseline["isolated"] == ["extra", "m"]
    assert baseline["total_cost"] == 5
    rng = random.Random(71)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        rng.shuffle(shuffled_nodes)
        cost_items = list(costs.items())
        rng.shuffle(cost_items)
        result = plan(
            shuffled_nodes,
            rng.sample(failed, k=len(failed)),
            rng.sample(targets, k=len(targets)),
            dict(cost_items),
        ).model_dump()
        assert result == baseline


# --------------------------------------------------------------------- HTTP


def _payload(nodes, failed, targets, costs):
    return {
        "nodes": [
            {
                "name": n.name,
                "content_digest": n.content_digest,
                "dependency_names": n.dependency_names,
            }
            for n in nodes
        ],
        "failed_nodes": failed,
        "targets": targets,
        "isolation_costs": costs,
    }


def test_http_isolation_status_and_body():
    nodes = [
        node("core", "c"),
        node("util", "u", ["core"]),
        node("api", "a", ["util", "core"]),
        node("web", "w", ["api"]),
        node("deploy", "d", ["web"]),
    ]
    payload = _payload(
        nodes,
        ["core"],
        ["deploy"],
        {"util": 2, "api": 5, "web": 6},
    )
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"isolated": ["api"], "total_cost": 5}


def test_http_no_path_returns_empty_set():
    nodes = [node("f", "f"), node("a", "a", ["f"]), node("t", "t")]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f"], ["t"], {"a": 1}),
    )
    assert resp.status_code == 200
    assert resp.json() == {"isolated": [], "total_cost": 0}


def test_http_impossible_is_400():
    nodes = [node("f", "f"), node("t", "t", ["f"])]
    resp = client.post(
        "/failure-isolation", json=_payload(nodes, ["f"], ["t"], {})
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "isolation_impossible"


def test_http_duplicate_failed_node_is_400():
    nodes = [node("f", "f"), node("t", "t")]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f", "f"], ["t"], {}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_failed_node"


def test_http_duplicate_target_is_400():
    nodes = [node("f", "f"), node("t", "t")]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f"], ["t", "t"], {}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_target"


def test_http_unknown_failed_node_is_400():
    resp = client.post(
        "/failure-isolation",
        json=_payload([node("t", "t")], ["ghost"], ["t"], {}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_failed_node"


def test_http_unknown_target_is_400():
    resp = client.post(
        "/failure-isolation",
        json=_payload([node("f", "f")], ["f"], ["ghost"], {}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_unknown_candidate_is_400():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f"], ["t"], {"ghost": 1}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_candidate"


def test_http_overlap_is_400():
    resp = client.post(
        "/failure-isolation",
        json=_payload([node("a", "a")], ["a"], ["a"], {}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "failed_target_overlap"


def test_http_protected_candidate_is_400():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f"], ["t"], {"f": 1, "a": 2}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_candidate"


def test_http_too_many_candidates_is_400():
    names = [f"n{i:02d}" for i in range(MAX_CANDIDATES + 1)]
    nodes = [node("f", "f"), node("t", "t")] + [
        node(name, name) for name in names
    ]
    resp = client.post(
        "/failure-isolation",
        json=_payload(nodes, ["f"], ["t"], {name: 1 for name in names}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "too_many_candidates"


def test_http_cycle_is_400():
    nodes = [node("f", "f", ["t"]), node("t", "t", ["f"])]
    resp = client.post(
        "/failure-isolation", json=_payload(nodes, ["f"], ["t"], {})
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


def test_http_negative_cost_is_422():
    nodes = [
        node("f", "f"),
        node("a", "a", ["f"]),
        node("t", "t", ["a"]),
    ]
    payload = _payload(nodes, ["f"], ["t"], {"a": -1})
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 422


def test_http_empty_failed_nodes_is_422():
    nodes = [node("t", "t")]
    payload = _payload(nodes, [], ["t"], {})
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 422


def test_http_empty_targets_is_422():
    nodes = [node("f", "f")]
    payload = _payload(nodes, ["f"], [], {})
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 422


def test_http_missing_section_is_422():
    payload = _payload([node("a", "a")], ["a"], ["a"], {})
    del payload["isolation_costs"]
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    payload = _payload([node("a", "a")], ["a"], ["a"], {})
    payload["bogus"] = 1
    resp = client.post("/failure-isolation", json=payload)
    assert resp.status_code == 422
