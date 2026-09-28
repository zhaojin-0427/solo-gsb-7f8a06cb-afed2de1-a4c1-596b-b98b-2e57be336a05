"""Release-transition planning: switch one bundle into another.

Given the current build graph (same schema as /plans), a release *target*
node, the offered artifacts and two endpoint bundles (``current_ids`` and
``next_ids``), plan the minimum number of single-node artifact switches that
turn the current bundle into the next bundle while keeping every intermediate
bundle dependency-consistent.

Artifacts reuse the /release-bundles schema: a unique ``id``, the graph
``node`` they belong to, a ``fingerprint`` (metadata only here: it is never
recomputed or compared against the graph), a ``size`` (also metadata only)
and ``dependencies`` mapping each of the node's **direct** dependency names
to a non-empty list of acceptable artifact ids.

Closure and offer limits
------------------------
* The target and its transitive dependency closure contain at most
  ``MAX_CLOSURE_NODES`` (8) nodes.
* Every closure node is offered between ``MIN_ARTIFACTS_PER_NODE`` (1) and
  ``MAX_ARTIFACTS_PER_NODE`` (3) artifacts.
* Every offered artifact must belong to a closure node ("artifacts belong
  only to the closure").

Endpoint bundles
----------------
Both ``current_ids`` and ``next_ids`` must pick *exactly* one artifact for
each closure node, and for every chosen artifact the id selected for each
direct dependency must occur in that artifact's acceptable-id list.  An
endpoint that fails any of these conditions is an ``invalid_endpoint_bundle``
error rather than an impossible transition.

Transitions
-----------
A step replaces the artifact chosen for exactly one closure node with another
artifact offered for that same node.  The resulting bundle must again be
dependency-consistent, so a switch may have to go through transitional
artifacts and may even revisit an earlier artifact (a rollback) before the
goal becomes reachable.

The state graph is searched breadth first, which yields the minimum number of
steps; among shortest plans the full sequence of ``(node, id)`` pairs is
compared lexicographically (node first, then artifact id) and the smallest
one is returned.  When the goal state lies in a different compatibility
component, no transition exists and the response is ``possible=false`` with
empty steps; identical endpoints return ``possible=true`` with empty steps.

Determinism
-----------
All validation and searching goes through node-/id-sorted structures, so the
request payload order (including the two id lists) never changes the result.
"""
from __future__ import annotations

from app.models import (
    BundleArtifact,
    Node,
    ReleaseTransitionResponse,
    TransitionStep,
)
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


def _validate_endpoint(
    endpoint: str,
    ids: list[str],
    closure_sorted: list[str],
    graph: dict[str, list[str]],
    id_to_artifact: dict[str, BundleArtifact],
) -> dict[str, str]:
    """Validate one endpoint bundle; return node name -> selected artifact id.

    Raises PlanError("invalid_endpoint_bundle") on the first structural or
    consistency problem found for this endpoint.
    """
    label = f"{endpoint}_ids"

    # ---- every id must identify a supplied (closure) artifact -------------
    unknown_ids = sorted({rid for rid in ids if rid not in id_to_artifact})
    if unknown_ids:
        raise PlanError(
            "invalid_endpoint_bundle",
            f"The {label} bundle references artifact ids that were not supplied",
            {"endpoint": endpoint, "unknown_ids": unknown_ids},
        )

    # ---- ids must be unique within the endpoint ---------------------------
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for rid in ids:
        if rid in seen:
            duplicate_ids.add(rid)
        seen.add(rid)
    if duplicate_ids:
        raise PlanError(
            "invalid_endpoint_bundle",
            f"The {label} bundle contains duplicate artifact ids",
            {"endpoint": endpoint, "duplicate_ids": sorted(duplicate_ids)},
        )

    # ---- exactly one artifact per closure node ----------------------------
    chosen: dict[str, str] = {}
    duplicated_nodes: set[str] = set()
    for rid in ids:
        node_name = id_to_artifact[rid].node
        if node_name in chosen:
            duplicated_nodes.add(node_name)
        chosen[node_name] = rid
    missing_nodes = sorted(name for name in closure_sorted if name not in chosen)
    extra_nodes = sorted(duplicated_nodes)
    if missing_nodes or extra_nodes:
        details: dict[str, object] = {"endpoint": endpoint}
        if missing_nodes:
            details["missing_nodes"] = missing_nodes
        if extra_nodes:
            details["extra_nodes"] = extra_nodes
        raise PlanError(
            "invalid_endpoint_bundle",
            f"The {label} bundle must choose exactly one artifact for every "
            "closure node",
            details,
        )

    # ---- every dependency edge must point at an acceptable selected id -----
    inconsistencies: list[dict[str, str]] = []
    for node_name in closure_sorted:
        artifact = id_to_artifact[chosen[node_name]]
        for dep in graph[node_name]:
            if chosen[dep] not in artifact.dependencies[dep]:
                inconsistencies.append(
                    {
                        "node": node_name,
                        "dependency": dep,
                        "artifact_id": chosen[node_name],
                    }
                )
    if inconsistencies:
        inconsistencies.sort(
            key=lambda item: (item["node"], item["dependency"], item["artifact_id"])
        )
        raise PlanError(
            "invalid_endpoint_bundle",
            f"The {label} bundle is not dependency-consistent: a chosen "
            "artifact does not accept the id selected for a direct dependency",
            {"endpoint": endpoint, "inconsistencies": inconsistencies},
        )

    return chosen


def plan_release_transitions(
    nodes: list[Node],
    target: str,
    artifacts: list[BundleArtifact],
    current_ids: list[str],
    next_ids: list[str],
) -> ReleaseTransitionResponse:
    """Plan the lexicographically smallest minimum-step bundle switch."""
    graph, _contents, _order = validate_graph(nodes)

    # ---- validation: target resolves -------------------------------------
    if target not in graph:
        raise PlanError(
            "unknown_target",
            f"Target node is not in the graph: {target!r}",
            {"target": target},
        )

    # ---- validation: closure size limit -----------------------------------
    closure = _dependency_closure(graph, target)
    if len(closure) > MAX_CLOSURE_NODES:
        raise PlanError(
            "closure_too_large",
            f"The target closure has {len(closure)} nodes; at most "
            f"{MAX_CLOSURE_NODES} are supported",
            {"nodes": len(closure), "limit": MAX_CLOSURE_NODES},
        )
    closure_sorted = sorted(closure)

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

    # ---- validation: artifacts belong to closure nodes only ---------------
    outside = sorted(
        artifact.id for artifact in artifacts if artifact.node not in closure
    )
    if outside:
        raise PlanError(
            "artifact_outside_closure",
            "Every offered artifact must belong to a node in the target "
            "dependency closure",
            {"ids": outside},
        )

    # ---- validation: 1..3 artifacts per closure node ----------------------
    counts = {name: 0 for name in closure_sorted}
    for artifact in artifacts:
        counts[artifact.node] += 1
    bad_counts = [
        {"node": name, "artifacts": counts[name]}
        for name in closure_sorted
        if not MIN_ARTIFACTS_PER_NODE <= counts[name] <= MAX_ARTIFACTS_PER_NODE
    ]
    if bad_counts:
        raise PlanError(
            "invalid_artifact_count",
            "Each closure node must be offered between "
            f"{MIN_ARTIFACTS_PER_NODE} and {MAX_ARTIFACTS_PER_NODE} artifacts",
            {
                "nodes": bad_counts,
                "min": MIN_ARTIFACTS_PER_NODE,
                "max": MAX_ARTIFACTS_PER_NODE,
            },
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
            unknown_refs.update(rid for rid in acceptable if rid not in id_to_artifact)
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
                        {"artifact": artifact.id, "dependency": dep, "artifact_id": rid}
                    )
    if wrong_refs:
        wrong_refs.sort(
            key=lambda item: (item["artifact"], item["dependency"], item["artifact_id"])
        )
        raise PlanError(
            "invalid_artifact_reference",
            "An acceptable artifact id belongs to a different node than the "
            "dependency it is listed under",
            {"references": wrong_refs},
        )

    # ---- validation: both endpoint bundles are legal ----------------------
    current = _validate_endpoint(
        "current", current_ids, closure_sorted, graph, id_to_artifact
    )
    following = _validate_endpoint(
        "next", next_ids, closure_sorted, graph, id_to_artifact
    )

    index = {name: i for i, name in enumerate(closure_sorted)}
    start = tuple(current[name] for name in closure_sorted)
    goal = tuple(following[name] for name in closure_sorted)

    # Identical bundles: nothing to switch.
    if start == goal:
        return ReleaseTransitionResponse(possible=True, steps=[])

    # Artifacts offered per node (sorted), for generating candidate switches.
    offered: dict[str, list[str]] = {name: [] for name in closure_sorted}
    for artifact in artifacts:
        offered[artifact.node].append(artifact.id)
    for ids in offered.values():
        ids.sort()

    def compatible(state: tuple[str, ...]) -> bool:
        """Every chosen artifact accepts the id chosen for each direct dep."""
        for i, node_name in enumerate(closure_sorted):
            artifact = id_to_artifact[state[i]]
            for dep in graph[node_name]:
                if state[index[dep]] not in artifact.dependencies[dep]:
                    return False
        return True

    # ---- breadth-first search with lexicographic tie-breaking -------------
    # Each level maps state -> the lexicographically smallest path reaching it.
    # Parents are expanded in path order and edges in (node, id) order, so the
    # first arrival at a state wins the tie.  ``seen`` covers the start and
    # every earlier level, preventing any step that lengthens a shortest path.
    seen: set[tuple[str, ...]] = {start}
    level: dict[tuple[str, ...], tuple[tuple[str, str], ...]] = {start: ()}
    winning_path: tuple[tuple[str, str], ...] | None = None

    while level and winning_path is None:
        next_level: dict[tuple[str, ...], tuple[tuple[str, str], ...]] = {}
        for state, path in sorted(level.items(), key=lambda item: item[1]):
            for node_name in closure_sorted:
                i = index[node_name]
                for new_id in offered[node_name]:
                    if new_id == state[i]:
                        continue
                    candidate = state[:i] + (new_id,) + state[i + 1 :]
                    if candidate in seen or candidate in next_level:
                        continue
                    if not compatible(candidate):
                        continue
                    candidate_path = path + ((node_name, new_id),)
                    next_level[candidate] = candidate_path
                    if candidate == goal:
                        winning_path = candidate_path
        seen.update(next_level)
        level = next_level

    if winning_path is None:
        return ReleaseTransitionResponse(possible=False, steps=[])

    steps: list[TransitionStep] = []
    state = start
    for node_name, new_id in winning_path:
        i = index[node_name]
        state = state[:i] + (new_id,) + state[i + 1 :]
        bundle = {
            closure_sorted[j]: state[j] for j in range(len(closure_sorted))
        }
        steps.append(TransitionStep(node=node_name, id=new_id, bundle=bundle))
    return ReleaseTransitionResponse(possible=True, steps=steps)


__all__ = [
    "MAX_CLOSURE_NODES",
    "MAX_ARTIFACTS_PER_NODE",
    "MIN_ARTIFACTS_PER_NODE",
    "plan_release_transitions",
]
