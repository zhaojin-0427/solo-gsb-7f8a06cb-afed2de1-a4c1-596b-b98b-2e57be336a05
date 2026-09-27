"""Release-bundle selection with dependency-consistent artifacts.

Given the current build graph (same schema as /plans), a release *target*
node and at most 16 offered artifacts, choose the artifacts that make up a
self-contained release bundle for the target.

Each artifact carries:

* a unique ``id``;
* the graph ``node`` it was built for;
* the ``fingerprint`` recorded at build time;
* a positive integer ``size``;
* ``dependencies``: a map from each of the node's **direct** dependency
  names to a non-empty list of artifact ids that are acceptable for it.

Consistency model
-----------------
* Fingerprints are recomputed with the /plans rules; an artifact whose
  fingerprint differs from its node's current fingerprint is stale and can
  never be selected.
* A valid bundle covers exactly the target node and its transitive
  dependency closure with one artifact per node (shared dependencies count
  once), and nothing outside the closure.
* For every selected artifact, the id chosen for each direct dependency
  must occur in that artifact's acceptable-id list for that dependency.

Selection
---------
All subsets of the fingerprint-matching artifacts are examined (at most
2**16).  A subset is feasible when it covers every closure node exactly
once and every dependency edge points at an acceptable selected id.  The
optimum minimizes the total size; ties are broken by the lexicographically
smallest sorted list of artifact ids.  When no feasible subset exists the
response is ``ready=false`` with an empty id list and size 0.

Validation
----------
Beyond the usual graph checks (duplicate node, missing dependency, cycle):
unknown target node, duplicate artifact ids, artifacts referencing unknown
nodes, dependency maps whose keys differ from the node's direct
dependencies, acceptable ids that do not identify any artifact, and
acceptable ids whose artifact belongs to a different node than the mapped
dependency name all produce structured HTTP 400 errors.

Determinism
-----------
All validation and enumeration goes through id/node-sorted structures, so
the request payload order never changes the result.
"""
from __future__ import annotations

from app.models import BundleArtifact, Node, ReleaseBundleResponse
from app.planner import PlanError, compute_fingerprints, validate_graph

MAX_ARTIFACTS = 16


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


def plan_release_bundle(
    nodes: list[Node],
    target: str,
    artifacts: list[BundleArtifact],
) -> ReleaseBundleResponse:
    """Pick the size-optimal dependency-consistent bundle for ``target``."""
    graph, contents, order = validate_graph(nodes)

    # ---- validation: artifact count limit --------------------------------
    if len(artifacts) > MAX_ARTIFACTS:
        raise PlanError(
            "too_many_artifacts",
            f"{len(artifacts)} artifacts were supplied; at most "
            f"{MAX_ARTIFACTS} are supported",
            {"artifacts": len(artifacts), "limit": MAX_ARTIFACTS},
        )

    # ---- validation: target resolves -------------------------------------
    if target not in graph:
        raise PlanError(
            "unknown_target",
            f"Target node is not in the graph: {target!r}",
            {"target": target},
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

    # ---- fingerprints (in topological order) ------------------------------
    fingerprints = compute_fingerprints(graph, contents, order)
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
        wrong_refs.sort(key=lambda item: (item["artifact"], item["dependency"], item["artifact_id"]))
        raise PlanError(
            "invalid_artifact_reference",
            "An acceptable artifact id belongs to a different node than the "
            "dependency it is listed under",
            {"references": wrong_refs},
        )

    # ---- closure and fingerprint-matching candidates ----------------------
    closure = _dependency_closure(graph, target)
    candidates = sorted(
        (
            artifact
            for artifact in artifacts
            if artifact.node in closure
            and artifact.fingerprint == fingerprints[artifact.node]
        ),
        key=lambda artifact: (artifact.node, artifact.id),
    )

    # ---- exact selection over all candidate subsets -----------------------
    best_key: tuple[int, list[str]] | None = None
    for mask in range(1 << len(candidates)):
        chosen_by_node: dict[str, BundleArtifact] = {}
        total_size = 0
        valid = True
        for i, artifact in enumerate(candidates):
            if not mask & (1 << i):
                continue
            if artifact.node in chosen_by_node:
                valid = False  # two artifacts for the same node
                break
            chosen_by_node[artifact.node] = artifact
            total_size += artifact.size
        if not valid or set(chosen_by_node) != closure:
            continue
        if not all(
            chosen_by_node[dep].id in artifact.dependencies[dep]
            for artifact in chosen_by_node.values()
            for dep in graph[artifact.node]
        ):
            continue
        chosen_ids = sorted(artifact.id for artifact in chosen_by_node.values())
        key = (total_size, chosen_ids)
        if best_key is None or key < best_key:
            best_key = key

    if best_key is None:
        return ReleaseBundleResponse(ready=False, artifact_ids=[], total_size=0)
    total_size, chosen_ids = best_key
    return ReleaseBundleResponse(
        ready=True, artifact_ids=chosen_ids, total_size=total_size
    )


__all__ = ["MAX_ARTIFACTS", "plan_release_bundle"]
