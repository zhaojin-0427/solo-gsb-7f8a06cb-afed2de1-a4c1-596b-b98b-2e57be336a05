"""Cache-retention planning under a storage capacity limit.

Given the current build graph (same schema as /plans), a target node, a
per-node rebuild cost, a set of existing cached artifacts and a capacity,
choose exactly which artifacts to keep so that the target's total rebuild
cost is minimal.

Cost model
----------
* An artifact is a *candidate* only when its stored fingerprint equals the
  fingerprint recomputed from the current graph; stale artifacts can never
  be kept and are evicted.
* A kept node is usable as-is: it is not rebuilt and its dependencies are
  never visited (the kept artifact "cuts" the dependency walk).
* Every other node on the target's dependency closure is rebuilt
  recursively; nodes shared by several paths are rebuilt (and charged)
  only once.
* The total cost is the sum of rebuild costs over the rebuilt set.

Selection
---------
All subsets of candidates whose total size fits the capacity are examined
(at most 2**16, since more than 16 matching candidates are rejected).
The optimum minimizes the total rebuild cost; ties are broken by choosing
the lexicographically smallest sorted list of kept node names.  Note this
means a kept artifact that does not reduce the cost can still be retained
when its name makes the sorted kept-list lexicographically smaller.

Determinism
-----------
Candidates are processed in name order and every emitted list is sorted,
so the request payload order never changes the result.
"""
from __future__ import annotations

from app.models import Artifact, CacheRetentionResponse, Node
from app.planner import PlanError, compute_fingerprints, validate_graph

MAX_CANDIDATES = 16


def _rebuild_set(
    graph: dict[str, list[str]], target: str, kept: set[str]
) -> set[str]:
    """Nodes that must be rebuilt so ``target`` becomes available.

    Iterative DFS from ``target``; kept nodes are neither included nor
    expanded, and every node is collected at most once.
    """
    rebuilt: set[str] = set()
    stack = [target]
    while stack:
        name = stack.pop()
        if name in kept or name in rebuilt:
            continue
        rebuilt.add(name)
        stack.extend(graph[name])
    return rebuilt


def plan_cache_retention(
    nodes: list[Node],
    target: str,
    rebuild_costs: dict[str, int],
    artifacts: list[Artifact],
    capacity: int,
) -> CacheRetentionResponse:
    """Pick the cost-optimal set of artifacts to keep within ``capacity``."""
    graph, contents, order = validate_graph(nodes)

    # ---- validation: target and rebuild-cost references -----------------
    if target not in graph:
        raise PlanError(
            "unknown_target",
            f"Target node is not in the graph: {target!r}",
            {"target": target},
        )
    unknown_costs = sorted(set(rebuild_costs) - set(graph))
    if unknown_costs:
        raise PlanError(
            "unknown_rebuild_cost",
            "Rebuild costs reference nodes that are not in the graph",
            {"names": unknown_costs},
        )
    missing_costs = sorted(set(graph) - set(rebuild_costs))
    if missing_costs:
        raise PlanError(
            "missing_rebuild_cost",
            "Every graph node needs a rebuild cost; some are missing",
            {"names": missing_costs},
        )

    # ---- validation: artifact names are unique and reference real nodes --
    seen: set[str] = set()
    duplicates: set[str] = set()
    for artifact in artifacts:
        if artifact.name in seen:
            duplicates.add(artifact.name)
        seen.add(artifact.name)
    if duplicates:
        raise PlanError(
            "duplicate_artifact",
            "Multiple artifacts supplied for the same node",
            {"names": sorted(duplicates)},
        )
    unknown_artifacts = sorted(seen - set(graph))
    if unknown_artifacts:
        raise PlanError(
            "unknown_artifact",
            "Artifacts reference nodes that are not in the graph",
            {"names": unknown_artifacts},
        )

    # ---- candidates: stored fingerprint must match the recomputed one ---
    fingerprints = compute_fingerprints(graph, contents, order)
    candidates = sorted(
        (a for a in artifacts if a.fingerprint == fingerprints[a.name]),
        key=lambda a: a.name,
    )
    if len(candidates) > MAX_CANDIDATES:
        raise PlanError(
            "too_many_candidates",
            f"{len(candidates)} artifacts match the current graph; "
            f"at most {MAX_CANDIDATES} are supported",
            {"candidates": len(candidates), "limit": MAX_CANDIDATES},
        )

    # ---- exact selection over all capacity-fitting subsets --------------
    best_key: tuple[int, list[str]] | None = None
    best_kept: list[str] = []
    best_rebuilt: set[str] = set()
    for mask in range(1 << len(candidates)):
        kept_names: list[str] = []  # candidates are name-sorted -> stays sorted
        total_size = 0
        for i, candidate in enumerate(candidates):
            if mask & (1 << i):
                kept_names.append(candidate.name)
                total_size += candidate.size
        if total_size > capacity:
            continue
        rebuilt = _rebuild_set(graph, target, set(kept_names))
        key = (sum(rebuild_costs[name] for name in rebuilt), kept_names)
        if best_key is None or key < best_key:
            best_key = key
            best_kept = kept_names
            best_rebuilt = rebuilt

    kept_set = set(best_kept)
    return CacheRetentionResponse(
        kept=best_kept,
        evicted=sorted(a.name for a in artifacts if a.name not in kept_set),
        rebuild=sorted(best_rebuilt),
        total_cost=best_key[0],
    )


__all__ = ["MAX_CANDIDATES", "plan_cache_retention"]
