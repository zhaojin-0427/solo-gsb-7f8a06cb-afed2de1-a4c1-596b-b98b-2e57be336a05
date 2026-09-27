"""Tests for the /release-policies endpoint and its evaluation core."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Node
from app.planner import PlanError
from app.policies import evaluate_release_policy

client = TestClient(app)


# ---------------------------------------------------------------- helpers


def node(name: str, content: str, deps: list[str] | None = None) -> Node:
    return Node(name=name, content_digest=content, dependency_names=deps or [])


# The README example graph: web -> api -> {util, core, testkit}; util -> core.
# The api -> testkit and util -> core edges are build_only, so the bundled
# closure of web is {web, api, util, core} and testkit never applies.
def readme_nodes() -> list[Node]:
    return [
        node("core", "core-src-v1"),
        node("util", "util-src-v1", ["core"]),
        node("api", "api-src-v1", ["util", "core", "testkit"]),
        node("web", "web-src-v1", ["api"]),
        node("testkit", "testkit-src-v1"),
    ]


def readme_edge_types() -> dict[str, dict[str, str]]:
    return {
        "core": {},
        "util": {"core": "build_only"},
        "api": {"util": "bundled", "core": "bundled", "testkit": "build_only"},
        "web": {"api": "bundled"},
        "testkit": {},
    }


def readme_labels() -> dict[str, list[str]]:
    return {
        "api": ["privacy-review"],
        "util": ["gpl-3.0"],
        "core": ["proprietary"],
        "testkit": ["export-control"],
    }


# Diamond for path tests: t -> a, t -> b, a -> z, b -> z (all bundled).
def diamond_nodes() -> list[Node]:
    return [
        node("z", "z"),
        node("a", "a", ["z"]),
        node("b", "b", ["z"]),
        node("t", "t", ["a", "b"]),
    ]


def diamond_edge_types() -> dict[str, dict[str, str]]:
    return {
        "z": {},
        "a": {"z": "bundled"},
        "b": {"z": "bundled"},
        "t": {"a": "bundled", "b": "bundled"},
    }


# -------------------------------------------------------------- evaluation


def test_allowed_when_everything_fulfilled_and_no_conflicts():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[],
    )
    assert result.allowed is True
    assert result.missing == []
    assert result.conflicts == []


def test_build_only_subgraph_never_enters_the_closure():
    # testkit carries an unfulfilled tag and half of an exclusive pair, but
    # it is only reachable through a build_only edge, so none of it applies.
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[["export-control", "gpl-3.0"]],
    )
    assert result.allowed is True
    assert result.missing == []
    assert result.conflicts == []


def test_disconnected_nodes_never_enter_the_closure():
    nodes = readme_nodes() + [node("side", "side-src-v1")]
    edge_types = {**readme_edge_types(), "side": {}}
    labels = {**readme_labels(), "side": ["unfulfilled"]}
    result = evaluate_release_policy(
        nodes,
        "web",
        edge_types,
        labels=labels,
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[],
    )
    assert result.allowed is True


def test_build_only_edge_at_the_target_cuts_the_dependency():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        {**readme_edge_types(), "web": {"api": "build_only"}},
        labels=readme_labels(),
        fulfilled=[],
        exclusive_pairs=[],
    )
    # Only web itself is in the closure, and web carries no tags.
    assert result.allowed is True
    assert result.missing == []
    assert result.conflicts == []


def test_missing_tag_blocks_release_and_reports_path():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "proprietary"],
        exclusive_pairs=[],
    )
    assert result.allowed is False
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "gpl-3.0", "path": ["web", "api", "util"]}
    ]
    assert result.conflicts == []


def test_tag_on_target_reports_single_node_path():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels={"web": ["self-cert"]},
        fulfilled=[],
        exclusive_pairs=[],
    )
    assert result.allowed is False
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "self-cert", "path": ["web"]}
    ]


def test_shortest_path_prefers_fewest_edges():
    # The tag sits on a (1 edge) and on z (2 edges): the 1-edge path wins.
    result = evaluate_release_policy(
        diamond_nodes(),
        "t",
        diamond_edge_types(),
        labels={"a": ["x"], "z": ["x"]},
        fulfilled=[],
        exclusive_pairs=[],
    )
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "x", "path": ["t", "a"]}
    ]


def test_equal_length_paths_broken_by_node_name_sequence():
    # z is reachable via ["t", "a", "z"] and ["t", "b", "z"]; the
    # lexicographically smaller node-name sequence wins.
    result = evaluate_release_policy(
        diamond_nodes(),
        "t",
        diamond_edge_types(),
        labels={"z": ["x"]},
        fulfilled=[],
        exclusive_pairs=[],
    )
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "x", "path": ["t", "a", "z"]}
    ]


def test_tag_on_several_nodes_uses_lexicographically_smallest_path():
    result = evaluate_release_policy(
        diamond_nodes(),
        "t",
        diamond_edge_types(),
        labels={"a": ["x"], "b": ["x"]},
        fulfilled=[],
        exclusive_pairs=[],
    )
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "x", "path": ["t", "a"]}
    ]


def test_conflict_blocks_and_reports_a_path_per_end():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[["gpl-3.0", "proprietary"]],
    )
    assert result.allowed is False
    assert result.missing == []
    assert [entry.model_dump() for entry in result.conflicts] == [
        {
            "pair": ["gpl-3.0", "proprietary"],
            "paths": {
                "gpl-3.0": ["web", "api", "util"],
                "proprietary": ["web", "api", "core"],
            },
        }
    ]


def test_conflict_applies_even_when_both_tags_are_fulfilled():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[["proprietary", "gpl-3.0"]],  # order normalized
    )
    assert result.allowed is False
    assert result.missing == []
    assert [entry.pair for entry in result.conflicts] == [
        ["gpl-3.0", "proprietary"]
    ]


def test_conflict_with_both_tags_on_the_same_node():
    result = evaluate_release_policy(
        diamond_nodes(),
        "t",
        diamond_edge_types(),
        labels={"z": ["x", "y"]},
        fulfilled=["x", "y"],
        exclusive_pairs=[["x", "y"]],
    )
    assert result.allowed is False
    assert [entry.model_dump() for entry in result.conflicts] == [
        {
            "pair": ["x", "y"],
            "paths": {"x": ["t", "a", "z"], "y": ["t", "a", "z"]},
        }
    ]


def test_pair_with_only_one_tag_present_does_not_conflict():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "gpl-3.0", "proprietary"],
        exclusive_pairs=[["gpl-3.0", "export-control"]],
    )
    assert result.allowed is True
    assert result.conflicts == []


def test_multiple_missing_and_conflicts_are_sorted():
    nodes = [
        node("p", "p"),
        node("q", "q"),
        node("t", "t", ["p", "q"]),
    ]
    edge_types = {
        "p": {},
        "q": {},
        "t": {"p": "bundled", "q": "bundled"},
    }
    labels = {"p": ["zz", "bb"], "q": ["aa", "bb"]}
    result = evaluate_release_policy(
        nodes,
        "t",
        edge_types,
        labels=labels,
        fulfilled=[],
        exclusive_pairs=[["aa", "zz"], ["bb", "zz"]],
    )
    assert result.allowed is False
    assert [entry.tag for entry in result.missing] == ["aa", "bb", "zz"]
    assert [entry.pair for entry in result.conflicts] == [
        ["aa", "zz"],
        ["bb", "zz"],
    ]
    assert [entry.model_dump() for entry in result.missing] == [
        {"tag": "aa", "path": ["t", "q"]},
        {"tag": "bb", "path": ["t", "p"]},
        {"tag": "zz", "path": ["t", "p"]},
    ]


def test_no_labels_at_all_is_allowed():
    result = evaluate_release_policy(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels={},
        fulfilled=[],
        exclusive_pairs=[],
    )
    assert result.allowed is True
    assert result.missing == []
    assert result.conflicts == []


# ------------------------------------------------------------------ errors


def test_unknown_target_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(), "ghost", readme_edge_types(), {}, [], []
        )
    assert exc.value.code == "unknown_target"
    assert exc.value.details == {"target": "ghost"}


def test_edge_types_missing_node_error():
    edge_types = readme_edge_types()
    del edge_types["web"]
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(), "web", edge_types, {}, [], []
        )
    assert exc.value.code == "invalid_edge_types"
    assert exc.value.details == {"missing_nodes": ["web"]}


def test_edge_types_unexpected_node_error():
    edge_types = {**readme_edge_types(), "ghost": {}}
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(), "web", edge_types, {}, [], []
        )
    assert exc.value.code == "invalid_edge_types"
    assert exc.value.details == {"unexpected_nodes": ["ghost"]}


def test_edge_types_missing_and_unexpected_edges_error():
    edge_types = readme_edge_types()
    del edge_types["api"]["util"]  # api directly depends on util
    edge_types["api"]["docs"] = "bundled"  # api does not depend on docs
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(), "web", edge_types, {}, [], []
        )
    assert exc.value.code == "invalid_edge_types"
    assert exc.value.details == {
        "edges": [{"node": "api", "missing": ["util"], "unexpected": ["docs"]}]
    }


def test_edge_types_report_multiple_bad_nodes_sorted():
    edge_types = readme_edge_types()
    edge_types["web"] = {}  # missing api
    edge_types["util"] = {"core": "bundled", "extra": "build_only"}
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(), "web", edge_types, {}, [], []
        )
    assert exc.value.code == "invalid_edge_types"
    assert exc.value.details == {
        "edges": [
            {"node": "util", "unexpected": ["extra"]},
            {"node": "web", "missing": ["api"]},
        ]
    }


def test_unknown_label_node_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(),
            "web",
            readme_edge_types(),
            labels={"ghost": ["x"]},
            fulfilled=[],
            exclusive_pairs=[],
        )
    assert exc.value.code == "unknown_label_node"
    assert exc.value.details == {"names": ["ghost"]}


def test_duplicate_label_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(),
            "web",
            readme_edge_types(),
            labels={"api": ["x", "y", "x"], "util": ["z", "z"]},
            fulfilled=[],
            exclusive_pairs=[],
        )
    assert exc.value.code == "duplicate_label"
    assert exc.value.details == {"labels": {"api": ["x"], "util": ["z"]}}


def test_duplicate_fulfilled_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(),
            "web",
            readme_edge_types(),
            labels={},
            fulfilled=["a", "b", "a"],
            exclusive_pairs=[],
        )
    assert exc.value.code == "duplicate_fulfilled"
    assert exc.value.details == {"tags": ["a"]}


def test_self_exclusive_pair_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(),
            "web",
            readme_edge_types(),
            labels={},
            fulfilled=[],
            exclusive_pairs=[["x", "x"]],
        )
    assert exc.value.code == "invalid_exclusive_pair"
    assert exc.value.details == {"self_pairs": [["x", "x"]]}


def test_repeated_exclusive_pair_error():
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            readme_nodes(),
            "web",
            readme_edge_types(),
            labels={},
            fulfilled=[],
            exclusive_pairs=[["a", "b"], ["b", "a"]],
        )
    assert exc.value.code == "invalid_exclusive_pair"
    assert exc.value.details == {"duplicate_pairs": [["a", "b"]]}


def test_graph_errors_are_reused():
    edge_types = {"a": {"b": "bundled"}, "b": {"a": "bundled"}}
    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            [node("a", "a", ["b"]), node("b", "b", ["a"])],
            "a",
            edge_types,
            {},
            [],
            [],
        )
    assert exc.value.code == "cycle_detected"

    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            [node("a", "1"), node("a", "2")], "a", {"a": {}}, {}, [], []
        )
    assert exc.value.code == "duplicate_node"

    with pytest.raises(PlanError) as exc:
        evaluate_release_policy(
            [node("a", "a", ["ghost"])], "a", {"a": {}}, {}, [], []
        )
    assert exc.value.code == "missing_dependency"


# -------------------------------------------------------------- determinism


def test_request_order_does_not_affect_result():
    nodes = readme_nodes()
    labels = readme_labels()
    fulfilled = ["privacy-review", "proprietary"]
    pairs = [["gpl-3.0", "proprietary"], ["export-control", "gpl-3.0"]]
    baseline = evaluate_release_policy(
        nodes, "web", readme_edge_types(), labels, fulfilled, pairs
    ).model_dump()

    rng = random.Random(23)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        rng.shuffle(shuffled_nodes)
        shuffled_edges = dict(
            rng.sample(list(readme_edge_types().items()), len(readme_edge_types()))
        )
        shuffled_edges = {
            name: dict(rng.sample(list(deps.items()), len(deps)))
            for name, deps in shuffled_edges.items()
        }
        shuffled_labels = dict(
            rng.sample(list(labels.items()), len(labels))
        )
        shuffled_fulfilled = fulfilled[:]
        rng.shuffle(shuffled_fulfilled)
        shuffled_pairs = [pair[:] for pair in pairs]
        for pair in shuffled_pairs:
            rng.shuffle(pair)
        rng.shuffle(shuffled_pairs)
        result = evaluate_release_policy(
            shuffled_nodes,
            "web",
            shuffled_edges,
            shuffled_labels,
            shuffled_fulfilled,
            shuffled_pairs,
        ).model_dump()
        assert result == baseline


# --------------------------------------------------------------------- HTTP


def _payload(nodes, target, edge_types, labels=None, fulfilled=None, pairs=None):
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
        "edge_types": edge_types,
        "labels": labels if labels is not None else {},
        "fulfilled": fulfilled if fulfilled is not None else [],
        "exclusive_pairs": pairs if pairs is not None else [],
    }


def _readme_payload():
    return _payload(
        readme_nodes(),
        "web",
        readme_edge_types(),
        labels=readme_labels(),
        fulfilled=["privacy-review", "proprietary"],
        pairs=[["gpl-3.0", "proprietary"]],
    )


def test_http_release_policy_blocked():
    resp = client.post("/release-policies", json=_readme_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "allowed": False,
        "missing": [{"tag": "gpl-3.0", "path": ["web", "api", "util"]}],
        "conflicts": [
            {
                "pair": ["gpl-3.0", "proprietary"],
                "paths": {
                    "gpl-3.0": ["web", "api", "util"],
                    "proprietary": ["web", "api", "core"],
                },
            }
        ],
    }


def test_http_release_policy_allowed():
    payload = _readme_payload()
    payload["fulfilled"] = ["privacy-review", "gpl-3.0", "proprietary"]
    payload["exclusive_pairs"] = []
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"allowed": True, "missing": [], "conflicts": []}


def test_http_unknown_target_is_400():
    payload = _readme_payload()
    payload["target"] = "ghost"
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_target"


def test_http_invalid_edge_types_is_400():
    payload = _readme_payload()
    del payload["edge_types"]["api"]["util"]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "invalid_edge_types"
    assert body["details"] == {"edges": [{"node": "api", "missing": ["util"]}]}


def test_http_unknown_label_node_is_400():
    payload = _readme_payload()
    payload["labels"]["ghost"] = ["x"]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_label_node"


def test_http_duplicate_label_is_400():
    payload = _readme_payload()
    payload["labels"]["api"] = ["privacy-review", "privacy-review"]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_label"


def test_http_duplicate_fulfilled_is_400():
    payload = _readme_payload()
    payload["fulfilled"] = ["a", "a"]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_fulfilled"


def test_http_invalid_exclusive_pair_is_400():
    payload = _readme_payload()
    payload["exclusive_pairs"] = [["x", "x"]]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_exclusive_pair"


def test_http_cycle_is_400():
    payload = _readme_payload()
    payload["nodes"] = [
        {"name": "a", "content_digest": "a", "dependency_names": ["b"]},
        {"name": "b", "content_digest": "b", "dependency_names": ["a"]},
    ]
    payload["edge_types"] = {"a": {"b": "bundled"}, "b": {"a": "bundled"}}
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "cycle_detected"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["edge_types"]["web"].__setitem__("api", "vendored"),
        lambda p: p["labels"]["api"].append(""),
        lambda p: p["fulfilled"].append("  "),
        lambda p: p["exclusive_pairs"].append(["only-one"]),
        lambda p: p["exclusive_pairs"].append(["a", "b", "c"]),
        lambda p: p["exclusive_pairs"].append(["a", ""]),
        lambda p: p.__setitem__("target", ""),
    ],
)
def test_http_invalid_fields_are_422(mutate):
    payload = _readme_payload()
    mutate(payload)
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 422


def test_http_unknown_field_rejected():
    payload = _readme_payload()
    payload["bogus"] = 1
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("section", ["nodes", "target", "edge_types"])
def test_http_missing_section_rejected(section):
    payload = _readme_payload()
    del payload[section]
    resp = client.post("/release-policies", json=payload)
    assert resp.status_code == 422
