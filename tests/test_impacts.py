"""Tests for the /impacts change-impact analysis."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.impacts import analyze_impacts
from app.main import app
from app.models import Node
from app.planner import PlanError

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


def payload(before, after):
    def dump(nodes):
        return [
            {
                "name": n.name,
                "content_digest": n.content_digest,
                "dependency_names": n.dependency_names,
            }
            for n in nodes
        ]

    return {"before": dump(before), "after": dump(after)}


# ------------------------------------------------------------- root causes


def test_deleted_nodes_are_listed_sorted():
    result = analyze_impacts(
        [node("b", "1"), node("a", "1"), node("c", "1")],
        [node("b", "1")],
    )
    assert result.deleted_nodes == ["a", "c"]
    assert result.root_causes == {}
    assert result.impacts == {}


def test_added_node_is_root_cause_and_affects_dependents():
    result = analyze_impacts(
        [node("app", "1")],
        [node("lib", "1"), node("app", "1", ["lib"])],
    )
    assert result.deleted_nodes == []
    assert result.root_causes["lib"].changes == ["added"]
    # app's dependency set changed too ([] -> ["lib"]).
    assert result.root_causes["app"].changes == ["dependencies_changed"]
    assert result.impacts["lib"]["lib"].path == ["lib"]
    assert result.impacts["app"]["lib"].path == ["lib", "app"]
    assert result.impacts["app"]["app"].path == ["app"]


def test_content_change_propagates_to_transitive_dependents():
    before = [
        node("a", "v1"),
        node("b", "b", ["a"]),
        node("c", "c", ["b"]),
        node("d", "d"),
    ]
    after = [
        node("a", "v2"),
        node("b", "b", ["a"]),
        node("c", "c", ["b"]),
        node("d", "d"),
    ]
    result = analyze_impacts(before, after)
    assert set(result.root_causes) == {"a"}
    assert result.root_causes["a"].changes == ["content_changed"]
    assert set(result.impacts) == {"a", "b", "c"}  # d untouched
    assert result.impacts["a"]["a"].path == ["a"]
    assert result.impacts["b"]["a"].path == ["a", "b"]
    assert result.impacts["c"]["a"].path == ["a", "b", "c"]


def test_dependency_set_change_detected_regardless_of_order():
    before = [node("a", "a"), node("b", "b"), node("x", "x", ["a", "b"])]
    after = [node("a", "a"), node("b", "b"), node("x", "x", ["b"])]
    result = analyze_impacts(before, after)
    assert result.root_causes["x"].changes == ["dependencies_changed"]

    # Merely reordering the same dependency set is not a change.
    same = analyze_impacts(before, [node("a", "a"), node("b", "b"), node("x", "x", ["b", "a"])])
    assert same.root_causes == {}
    assert same.impacts == {}


def test_node_can_carry_multiple_change_types():
    before = [node("a", "a"), node("x", "old", ["a"])]
    after = [node("a", "a"), node("x", "new", [])]
    result = analyze_impacts(before, after)
    assert result.root_causes["x"].changes == ["content_changed", "dependencies_changed"]


def test_no_changes_yields_empty_result():
    nodes = [node("a", "a"), node("b", "b", ["a"])]
    result = analyze_impacts(nodes, nodes)
    assert result.deleted_nodes == []
    assert result.root_causes == {}
    assert result.impacts == {}


def test_empty_graphs():
    result = analyze_impacts([], [])
    assert result.deleted_nodes == []
    assert result.root_causes == {}
    assert result.impacts == {}


# ------------------------------------------------------------------- paths


def test_root_cause_path_to_self_contains_only_itself():
    result = analyze_impacts([node("a", "v1")], [node("a", "v2")])
    assert result.impacts["a"]["a"].path == ["a"]


def test_shorter_path_wins_over_lexicographically_smaller_longer_one():
    # z is reachable via [r, z] and [r, a, z]; the short path must win even
    # though ["r", "a", "z"] sorts before ["r", "z"] element-wise... it does
    # not matter: length dominates.
    after = [
        node("r", "r"),
        node("a", "a", ["r"]),
        node("z", "z", ["r", "a"]),
    ]
    result = analyze_impacts([], after)
    assert result.impacts["z"]["r"].path == ["r", "z"]


def test_equal_length_paths_tie_broken_by_name_sequence():
    # t reachable from r via m1 or m2 (both length 3); ["r","m1","t"] wins.
    after = [
        node("r", "r"),
        node("m2", "m2", ["r"]),
        node("m1", "m1", ["r"]),
        node("t", "t", ["m1", "m2"]),
    ]
    result = analyze_impacts([], after)
    assert result.impacts["t"]["r"].path == ["r", "m1", "t"]


def test_tie_break_compares_full_sequence_not_next_hop_only():
    # Both paths to t have length 4; compare element-wise: "y" < "z" at the
    # second position, so [r, y, a, t] wins even though "a" < "b" later.
    after = [
        node("r", "r"),
        node("y", "y", ["r"]),
        node("z", "z", ["r"]),
        node("a", "a", ["y"]),
        node("b", "b", ["z"]),
        node("t", "t", ["a", "b"]),
    ]
    result = analyze_impacts([], after)
    assert result.impacts["t"]["r"].path == ["r", "y", "a", "t"]


def test_each_root_cause_reported_separately_per_node():
    # a changed content; b (dependent of a) changed content too.
    before = [node("a", "v1"), node("b", "v1", ["a"]), node("c", "c", ["b"])]
    after = [node("a", "v2"), node("b", "v2", ["a"]), node("c", "c", ["b"])]
    result = analyze_impacts(before, after)

    assert set(result.impacts["b"]) == {"a", "b"}
    assert result.impacts["b"]["a"].changes == ["content_changed"]
    assert result.impacts["b"]["a"].path == ["a", "b"]
    assert result.impacts["b"]["b"].path == ["b"]

    assert set(result.impacts["c"]) == {"a", "b"}
    assert result.impacts["c"]["a"].path == ["a", "b", "c"]
    assert result.impacts["c"]["b"].path == ["b", "c"]


def test_deleted_dependency_makes_dependent_a_root_cause():
    before = [node("old", "old"), node("x", "x", ["old"])]
    after = [node("x", "x")]
    result = analyze_impacts(before, after)
    assert result.deleted_nodes == ["old"]
    assert result.root_causes["x"].changes == ["dependencies_changed"]


# -------------------------------------------------------------- validation


@pytest.mark.parametrize("label", ["before", "after"])
def test_each_graph_validated_for_duplicates(label):
    good = [node("a", "a")]
    bad = [node("a", "1"), node("a", "2")]
    with pytest.raises(PlanError) as exc:
        analyze_impacts(bad, good) if label == "before" else analyze_impacts(good, bad)
    assert exc.value.code == "duplicate_node"
    assert exc.value.details["graph"] == label


@pytest.mark.parametrize("label", ["before", "after"])
def test_each_graph_validated_for_missing_dependencies(label):
    good = [node("a", "a")]
    bad = [node("a", "a", ["ghost"])]
    with pytest.raises(PlanError) as exc:
        analyze_impacts(bad, good) if label == "before" else analyze_impacts(good, bad)
    assert exc.value.code == "missing_dependency"
    assert exc.value.details["graph"] == label


@pytest.mark.parametrize("label", ["before", "after"])
def test_each_graph_validated_for_cycles(label):
    good = [node("a", "a")]
    bad = [node("a", "a", ["b"]), node("b", "b", ["a"])]
    with pytest.raises(PlanError) as exc:
        analyze_impacts(bad, good) if label == "before" else analyze_impacts(good, bad)
    assert exc.value.code == "cycle_detected"
    assert exc.value.details["graph"] == label


def test_before_graph_is_validated_first():
    bad = [node("a", "a", ["ghost"])]
    with pytest.raises(PlanError) as exc:
        analyze_impacts(bad, bad)
    assert exc.value.details["graph"] == "before"


# -------------------------------------------------------------- determinism


def test_input_order_does_not_affect_result():
    before = [
        node("core", "core-v1"),
        node("util", "util", ["core"]),
        node("api", "api", ["util", "core"]),
        node("web", "web", ["api"]),
        node("legacy", "legacy"),
    ]
    after = [
        node("core", "core-v2"),
        node("util", "util", ["core"]),
        node("api", "api", ["util"]),
        node("web", "web", ["api"]),
        node("metrics", "metrics", ["api"]),
    ]
    baseline = analyze_impacts(before, after).model_dump()
    rng = random.Random(11)
    for _ in range(10):
        shuffled_before = before[:]
        shuffled_after = after[:]
        for n in shuffled_before + shuffled_after:
            rng.shuffle(n.dependency_names)
        rng.shuffle(shuffled_before)
        rng.shuffle(shuffled_after)
        assert analyze_impacts(shuffled_before, shuffled_after).model_dump() == baseline


# --------------------------------------------------------------------- HTTP


def test_http_impacts_status_and_body():
    resp = client.post(
        "/impacts",
        json=payload(
            [node("a", "v1"), node("b", "b", ["a"]), node("old", "old")],
            [node("a", "v2"), node("b", "b", ["a"])],
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_nodes"] == ["old"]
    assert body["root_causes"] == {"a": {"changes": ["content_changed"]}}
    assert body["impacts"] == {
        "a": {"a": {"changes": ["content_changed"], "path": ["a"]}},
        "b": {"a": {"changes": ["content_changed"], "path": ["a", "b"]}},
    }


def test_http_impacts_invalid_before_graph_is_400_with_label():
    resp = client.post(
        "/impacts",
        json=payload([node("a", "a", ["ghost"])], [node("a", "a")]),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "missing_dependency"
    assert body["details"]["graph"] == "before"


def test_http_impacts_invalid_after_graph_is_400_with_label():
    resp = client.post(
        "/impacts",
        json=payload(
            [node("a", "a")],
            [node("a", "a", ["b"]), node("b", "b", ["a"])],
        ),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "cycle_detected"
    assert body["details"]["graph"] == "after"


def test_http_impacts_unknown_field_rejected():
    resp = client.post("/impacts", json={"before": [], "after": [], "bogus": 1})
    assert resp.status_code == 422


def test_http_impacts_missing_section_rejected():
    resp = client.post("/impacts", json={"before": []})
    assert resp.status_code == 422
