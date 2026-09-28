"""Release-bundle artifact transition planning.

Given the current build graph (same schema as /plans), a release *target*
node, the offered artifacts (same schema as /release-bundles) and the two
endpoint bundles ``current_ids`` and ``next_ids``, find the shortest
sequence of single-node artifact replacements that transforms the current
bundle into the next one while keeping every intermediate bundle
dependency-consistent.

Offer limits
------------
* The target's transitive dependency closure holds at most
  ``MAX_CLOSURE_NODES`` (8) nodes.
* Every closure node offers between ``MIN_ARTIFACTS_PER_NODE`` (1) and
  ``MAX_ARTIFACTS_PER_NODE`` (3) artifacts; artifacts may only belong to
  closure nodes (an artifact for a node outside the closure is an error).
* Fingerprints and sizes are carried as plain metadata: they are never
  recomputed or compared, so stale artifacts are usable as transitional
  stepping stones.

Consistency model
-----------------
A bundle chooses exactly one artifact per closure node.  It is consistent
when, for every chosen artifact, the id chosen for each direct dependency
occurs in that artifact's acceptable-id list for that dependency.  Both
endpoint bundles must be consistent, and so must every bundle visited on
the way: each step swaps the artifact of exactly one node for a *different*
artifact of that node, and the resulting bundle is only entered when it is
consistent.

Search
------
The state space is the cross product of the per-node artifact id sets
(``3**8`` states at most).  Breadth-first search over consistent states
yields the minimum number of swaps; neighbours are expanded in sorted
(node name, artifact id) order, which makes the first shortest path found
the lexicographically smallest full sequence of ``(node, id)`` pairs.
Transitional artifacts and revisiting an artifact id are allowed.

When no consistent path exists the response is ``possible=false`` with an
empty step list.  Identical endpoints give ``possible=true`` with no steps.

Validation
----------
Graph checks (duplicate node, missing dependency, cycle) are reused.  On
top of them: unknown target node, closures larger than eight nodes,
duplicate artifact ids, artifacts on unknown or out-of-closure nodes,
per-node offer counts outside 1..3, dependency maps that do not mirror the
node's direct dependencies, unknown acceptable-id references, references
pointing at an artifact of another node, and invalid endpoint bundles all
produce structured HTTP 400 errors.

Determinism
-----------
Everything goes through name/id-sorted structures; reordering the request
payload (nodes, artifacts or either endpoint id list) never changes the
result.
"""
from __future__ import annotations

from collections import Counter, deque

from app.models import BundleArtifact, Node, ReleaseTransitionResponse, ReleaseTransitionStep
from app.planner import PlanError, validate_graph

MAX_CLOSURE_NODES = 8
MIN_ARTIFACTS_PER_NODE = 1
MAX_ARTIFACTS_PER_NODE = 3


def _dependency_closure(graph: dict[str, list[str]], target: str) -> set[str]:
    """Target and every node reachable through direct dependencies."""
    closure: set[str] = set()
    stack = [target]
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        closure.add(name)
        stack.extend(graph[name])
    return closure


def _validate_offers(
    graph: dict[str, list[str]],
    target: str,
    artifacts: list[BundleArtifact],
) -> tuple[set[str], dict[str, BundleArtifact], dict[str, list[BundleArtifact]]]:
    """Validate target, closure and the artifact offer, returning indexes."""
    if target not in graph:
        raise PlanError(
            "unknown_target",
            f"Target node is not in the graph: {target!r}",
            {"target": target},
        )

    closure = _dependency_closure(graph, target)
    if len(closure) > MAX_CLOSURE_NODES:
        raise PlanError(
            "closure_too_large",
            f"The target dependency closure has {len(closure)} nodes; at most "
            f"{MAX_CLOSURE_NODES} are supported",
            {
                "target": target,
                "closure_size": len(closure),
                "limit": MAX_CLOSURE_NODES,
            },
        )

    # ---- validation: artifact ids are unique ------------------------------
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for artifact in artifacts:
        if artifact.id in seen_ids:
            duplicate_ids.add(artifact.id)
        seen_ids.add(artifact.id)
    if duplicate_ids:
        raise PlanError(
            "duplicate_artifact_id",
            "Artifact ids must be unique",
            {"ids": sorted(duplicate_ids)},
        )

    # ---- validation: artifacts reference real nodes ----------------------
    unknown_nodes = sorted({a.node for a in artifacts if a.node not in graph})
    if unknown_nodes:
        raise PlanError(
            "unknown_artifact_node",
            "Artifacts reference nodes that are not in the graph",
            {"names": unknown_nodes},
        )

    # ---- validation: artifacts belong to the closure only -----------------
    outside = sorted(a.id for a in artifacts if a.node not in closure)
    if outside:
        raise PlanError(
            "artifact_outside_closure",
            "Artifacts may only belong to nodes in the target dependency closure",
            {"ids": outside},
        )

    # ---- validation: each closure node offers 1..3 artifacts --------------
    by_node: dict[str, list[BundleArtifact]] = {name: [] for name in closure}
    for artifact in artifacts:
        by_node[artifact.node].append(artifact)
    bad_counts = [
        {
            "node": name,
            "count": len(by_node[name]),
            "min": MIN_ARTIFACTS_PER_NODE,
            "max": MAX_ARTIFACTS_PER_NODE,
        }
        for name in sorted(closure)
        if not (
            MIN_ARTIFACTS_PER_NODE
            <= len(by_node[name])
            <= MAX_ARTIFACTS_PER_NODE
        )
    ]
    if bad_counts:
        raise PlanError(
            "invalid_artifact_count",
            "Every closure node must offer between "
            f"{MIN_ARTIFACTS_PER_NODE} and {MAX_ARTIFACTS_PER_NODE} artifacts",
            {"nodes": bad_counts},
        )

    # ---- validation: dependency maps mirror the node's direct deps -------
    bad_maps: list[dict[str, object]] = []
    for artifact in sorted(artifacts, key=lambda a: a.id):
        deps = set(graph[artifact.node])
        mapped = set(artifact.dependencies)
        missing = sorted(deps - mapped)
        unexpected = sorted(mapped - deps)
        if missing or unexpected:
            entry: dict[str, object] = {"id": artifact.id}
            if missing:
                entry["missing"] = missing
            if unexpected:
                entry["unexpected"] = unexpected
            bad_maps.append(entry)
    if bad_maps:
        raise PlanError(
            "invalid_dependency_map",
            "Artifact dependency maps must list exactly the node's direct "
            "dependencies, each with a non-empty list of acceptable ids",
            {"artifacts": bad_maps},
        )

    id_to_artifact = {artifact.id: artifact for artifact in artifacts}

    # ---- validation: referenced ids exist ---------------------------------
    unknown_refs: set[str] = set()
    for artifact in artifacts:
        for acceptable in artifact.dependencies.values():
            unknown_refs.update(
                rid for rid in acceptable if rid not in id_to_artifact
            )
    if unknown_refs:
        raise PlanError(
            "unknown_artifact_reference",
            "Dependency lists reference artifact ids that were not supplied",
            {"ids": sorted(unknown_refs)},
        )

    # ---- validation: referenced id belongs to the mapped dependency node --
    wrong_refs: list[dict[str, str]] = []
    for artifact in sorted(artifacts, key=lambda a: a.id):
        for dep in sorted(artifact.dependencies):
            for rid in artifact.dependencies[dep]:
                if id_to_artifact[rid].node != dep:
                    wrong_refs.append(
                        {
                            "artifact": artifact.id,
                            "dependency": dep,
                            "artifact_id": rid,
                        }
                    )
    if wrong_refs:
        wrong_refs.sort(
            key=lambda item: (
                item["artifact"],
                item["dependency"],
                item["artifact_id"],
            )
        )
        raise PlanError(
            "invalid_artifact_reference",
            "An acceptable artifact id belongs to a different node than the "
            "dependency it is listed under",
            {"references": wrong_refs},
        )

    return closure, id_to_artifact, by_node


def _endpoint_problems(
    ids: list[str],
    closure: set[str],
    graph: dict[str, list[str]],
    id_to_artifact: dict[str, BundleArtifact],
) -> dict[str, object]:
    """Structural + consistency problems of one endpoint bundle, if any."""
    problems: dict[str, object] = {}

    unknown = sorted(rid for rid in ids if rid not in id_to_artifact)
    if unknown:
        problems["unknown"] = unknown

    counts = Counter(ids)
    duplicate = sorted(rid for rid, count in counts.items() if count > 1)
    if duplicate:
        problems["duplicate"] = duplicate

    coverage: Counter[str] = Counter(
        id_to_artifact[rid].node for rid in ids if rid in id_to_artifact
    )
    missing_nodes = sorted(name for name in closure if coverage.get(name, 0) == 0)
    if missing_nodes:
        problems["missing_nodes"] = missing_nodes
    multiple_nodes = sorted(name for name in closure if coverage[name] > 1)
    if multiple_nodes:
        problems["multiple_nodes"] = multiple_nodes

    # Dependency edges are only checkable when both endpoints select exactly
    # one artifact; other problems are already reported above.
    selected_for_node = {
        name: next(rid for rid in ids if id_to_artifact[rid].node == name)
        for name in closure
        if coverage.get(name, 0) == 1
    }
    inconsistent: list[dict[str, object]] = []
    for rid in sorted(ids):
        artifact = id_to_artifact.get(rid)
        if artifact is None:
            continue
        for dep in graph[artifact.node]:
            chosen = selected_for_node.get(dep)
            if chosen is None:
                continue
            if chosen not in artifact.dependencies[dep]:
                inconsistent.append(
                    {
                        "artifact": artifact.id,
                        "dependency": dep,
                        "selected": chosen,
                        "acceptable": sorted(artifact.dependencies[dep]),
                    }
                )
    if inconsistent:
        problems["inconsistent"] = inconsistent

    return problems


def plan_release_transition(
    nodes: list[Node],
    target: str,
    artifacts: list[BundleArtifact],
    current_ids: list[str],
    next_ids: list[str],
) -> ReleaseTransitionResponse:
    """Shortest single-node swap sequence between two consistent bundles."""
    graph, _contents, _order = validate_graph(nodes)
    closure, id_to_artifact, by_node = _validate_offers(
        graph, target, artifacts
    )

    # ---- validation: both endpoint bundles are legal ----------------------
    endpoint_problems: dict[str, dict[str, object]] = {}
    current_problems = _endpoint_problems(
        current_ids, closure, graph, id_to_artifact
    )
    if current_problems:
        endpoint_problems["current"] = current_problems
    next_problems = _endpoint_problems(
        next_ids, closure, graph, id_to_artifact
    )
    if next_problems:
        endpoint_problems["next"] = next_problems
    if endpoint_problems:
        raise PlanError(
            "invalid_endpoint_bundle",
            "current_ids and next_ids must each pick exactly one consistent "
            "artifact per closure node",
            endpoint_problems,
        )

    node_order = sorted(closure)
    offers = {
        name: sorted(
            (artifact.id for artifact in by_node[name]),
        )
        for name in node_order
    }

    def _state(bundle_ids: list[str]) -> tuple[str, ...]:
        chosen = {id_to_artifact[rid].node: rid for rid in bundle_ids}
        return tuple(chosen[name] for name in node_order)

    def _consistent(state: tuple[str, ...]) -> bool:
        assignment = dict(zip(node_order, state))
        for name, rid in zip(node_order, state):
            artifact = id_to_artifact[rid]
            for dep in graph[name]:
                if assignment[dep] not in artifact.dependencies[dep]:
                    return False
        return True

    start = _state(current_ids)
    goal = _state(next_ids)
    if start == goal:
        return ReleaseTransitionResponse(possible=True, steps=[])

    # ---- breadth-first search over consistent states ----------------------
    # Neighbours expand in (node name, artifact id) order, so the first
    # shortest path reached is the lexicographically smallest (node, id)
    # sequence.
    parents: dict[tuple[str, ...], tuple[str, ...] | None] = {start: None}
    moves: dict[tuple[str, ...], tuple[str, str]] = {}
    queue: deque[tuple[str, ...]] = deque([start])

    found: tuple[str, ...] | None = None
    while queue:
        state = queue.popleft()
        if state == goal:
            found = state
            break
        for index, name in enumerate(node_order):
            current = state[index]
            for candidate in offers[name]:
                if candidate == current:
                    continue
                neighbour = state[:index] + (candidate,) + state[index + 1 :]
                if neighbour in parents or not _consistent(neighbour):
                    continue
                parents[neighbour] = state
                moves[neighbour] = (name, candidate)
                queue.append(neighbour)

    if found is None:
        return ReleaseTransitionResponse(possible=False, steps=[])

    reversed_steps: list[tuple[str, str, tuple[str, ...]]] = []
    state = found
    while state != start:
        node_name, rid = moves[state]
        reversed_steps.append((node_name, rid, state))
        parent = parents[state]
        assert parent is not None
        state = parent

    steps = [
        ReleaseTransitionStep(
            node=node_name,
            id=rid,
            bundle={name: value for name, value in zip(node_order, bundle_state)},
        )
        for node_name, rid, bundle_state in reversed(reversed_steps)
    ]
    return ReleaseTransitionResponse(possible=True, steps=steps)


__all__ = [
    "MAX_CLOSURE_NODES",
    "MAX_ARTIFACTS_PER_NODE",
    "MIN_ARTIFACTS_PER_NODE",
    "plan_release_transition",
]
