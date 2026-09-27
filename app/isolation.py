"""Pre-release failure-isolation planning.

Given the current build graph (same schema as /plans), a non-empty set of
*failed* build nodes, a non-empty set of release *targets* and a set of
*isolatable* candidate nodes with non-negative integer isolation costs,
choose which nodes to isolate (remove) so that **every** path from any
failed node to any target is cut.

Path model
----------
Edges run from a dependency node to the nodes that depend on it — the
propagation direction — i.e. a failed node reaches every node that
transitively depends on it.  An isolated node is treated as removed from
the graph: no remaining path may pass through it.

Selection
---------
All subsets of the isolatable candidates are examined (at most 2**16,
since more than 16 candidates are rejected).  A subset is feasible when
removing it leaves no failed-node -> target path.  The optimum minimizes
the total isolation cost; ties are broken by choosing the lexicographically
smallest sorted list of isolated node names (the empty list precedes any
non-empty list).

When no failed node reaches any target to begin with, the empty set with
cost 0 is returned.  When no candidate subset can cut every path (the only
uncuttable path being one with no isolatable node on it, e.g. a failed
node that is a direct dependency of a target), a PlanError is raised.

Determinism
-----------
Inputs are validated and processed through name-sorted structures at every
step, so the request payload order never changes the result.
"""
from __future__ import annotations

from app.models import FailureIsolationResponse, Node
from app.planner import PlanError, validate_graph

MAX_CANDIDATES = 16


def _target_reachable(
    dependents: dict[str, list[str]],
    failed: set[str],
    targets: set[str],
    removed: frozenset[str],
) -> bool:
    """True when some failed node still reaches some target after removal.

    Iterative DFS over the dependency -> dependent edges, starting from
    every failed node; nodes in ``removed`` are never visited or expanded.
    """
    stack = [name for name in failed if name not in removed]
    seen: set[str] = set(stack)
    while stack:
        name = stack.pop()
        if name in targets:
            return True
        for dependent in dependents[name]:
            if dependent in removed or dependent in seen:
                continue
            seen.add(dependent)
            stack.append(dependent)
    return False


def _duplicates(names: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for name in names:
        if name in seen:
            dupes.add(name)
        seen.add(name)
    return sorted(dupes)


def plan_failure_isolation(
    nodes: list[Node],
    failed_nodes: list[str],
    targets: list[str],
    isolation_costs: dict[str, int],
) -> FailureIsolationResponse:
    """Pick the cost-optimal node set isolating every target from failures."""
    graph, _contents, _order = validate_graph(nodes)

    # ---- validation: non-empty unique failed/target sets -----------------
    failed_dupes = _duplicates(failed_nodes)
    if failed_dupes:
        raise PlanError(
            "duplicate_failed_node",
            "Failed node names must be unique",
            {"names": failed_dupes},
        )
    target_dupes = _duplicates(targets)
    if target_dupes:
        raise PlanError(
            "duplicate_target",
            "Target node names must be unique",
            {"names": target_dupes},
        )

    failed = set(failed_nodes)
    targets_set = set(targets)

    # ---- validation: references resolve against the graph ----------------
    unknown_failed = sorted(failed - set(graph))
    if unknown_failed:
        raise PlanError(
            "unknown_failed_node",
            "Failed nodes reference nodes that are not in the graph",
            {"names": unknown_failed},
        )
    unknown_targets = sorted(targets_set - set(graph))
    if unknown_targets:
        raise PlanError(
            "unknown_target",
            "Targets reference nodes that are not in the graph",
            {"names": unknown_targets},
        )

    # ---- validation: a node cannot be both failed and a release target ---
    overlap = sorted(failed & targets_set)
    if overlap:
        raise PlanError(
            "failed_target_overlap",
            "A node cannot be both a failed node and a release target",
            {"names": overlap},
        )

    # ---- validation: isolatable candidates resolve and are not protected -
    unknown_candidates = sorted(set(isolation_costs) - set(graph))
    if unknown_candidates:
        raise PlanError(
            "unknown_candidate",
            "Isolatable nodes reference nodes that are not in the graph",
            {"names": unknown_candidates},
        )
    protected = sorted(set(isolation_costs) & (failed | targets_set))
    if protected:
        raise PlanError(
            "invalid_candidate",
            "Failed nodes and release targets cannot be isolated",
            {"names": protected},
        )
    if len(isolation_costs) > MAX_CANDIDATES:
        raise PlanError(
            "too_many_candidates",
            f"{len(isolation_costs)} isolatable nodes were supplied; "
            f"at most {MAX_CANDIDATES} are supported",
            {"candidates": len(isolation_costs), "limit": MAX_CANDIDATES},
        )

    # ---- reverse edges: dependency -> nodes that directly depend on it ---
    dependents: dict[str, list[str]] = {name: [] for name in graph}
    for name, deps in graph.items():
        for dep in deps:
            dependents[dep].append(name)
    for name in dependents:
        dependents[name].sort()

    # ---- nothing to do when no failure currently reaches a target --------
    if not _target_reachable(dependents, failed, targets_set, frozenset()):
        return FailureIsolationResponse(isolated=[], total_cost=0)

    # ---- exact selection over all candidate subsets ----------------------
    candidates = sorted(isolation_costs)
    best_key: tuple[int, list[str]] | None = None
    for mask in range(1 << len(candidates)):
        isolated = [candidates[i] for i in range(len(candidates)) if mask & (1 << i)]
        removed = frozenset(isolated)
        if _target_reachable(dependents, failed, targets_set, removed):
            continue
        key = (sum(isolation_costs[name] for name in isolated), isolated)
        if best_key is None or key < best_key:
            best_key = key

    if best_key is None:
        raise PlanError(
            "isolation_impossible",
            "No subset of the isolatable nodes cuts every failed-node to "
            "target path; failed nodes and targets themselves cannot be isolated",
            {"failed_nodes": sorted(failed), "targets": sorted(targets_set)},
        )

    cost, isolated = best_key
    return FailureIsolationResponse(isolated=isolated, total_cost=cost)


__all__ = ["MAX_CANDIDATES", "plan_failure_isolation"]
