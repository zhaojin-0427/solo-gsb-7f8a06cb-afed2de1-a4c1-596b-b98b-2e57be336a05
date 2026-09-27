"""Change-impact analysis between two dependency graph snapshots.

Given a *before* and an *after* graph (same node schema as /plans), the
analysis:

* lists nodes present only in *before* (deleted nodes);
* flags root causes in *after*: added nodes, content-digest changes and
  direct dependency-set changes — one node may carry several change kinds;
* propagates each root cause along the *after* graph's dependency edges (a
  change in a dependency affects its dependents) and reports, for every
  affected node and every root cause reaching it, the root cause's change
  kinds plus the shortest propagation path.  Equal-length path ties are
  broken by lexicographic order of the node-name sequence, and a root
  cause's path to itself contains only itself.

Both graphs are validated independently (duplicate names, missing
dependencies, cycles) and every emitted structure is name-sorted, so the
input order of nodes/dependencies never changes the result.
"""
from __future__ import annotations

from app.models import ImpactEntry, ImpactResponse, Node, RootCause
from app.planner import validate_graph

CHANGE_ADDED = "added"
CHANGE_CONTENT = "content_changed"
CHANGE_DEPENDENCIES = "dependencies_changed"


def _root_causes(
    before_graph: dict[str, list[str]],
    before_contents: dict[str, str],
    after_graph: dict[str, list[str]],
    after_contents: dict[str, str],
) -> dict[str, list[str]]:
    """Change kinds per new-graph node; only nodes with at least one kind."""
    causes: dict[str, list[str]] = {}
    for name in sorted(after_graph):
        kinds: list[str] = []
        if name not in before_graph:
            kinds.append(CHANGE_ADDED)
        else:
            if before_contents[name] != after_contents[name]:
                kinds.append(CHANGE_CONTENT)
            if set(before_graph[name]) != set(after_graph[name]):
                kinds.append(CHANGE_DEPENDENCIES)
        if kinds:
            causes[name] = sorted(kinds)
    return causes


def _shortest_paths(root: str, dependents: dict[str, list[str]]) -> dict[str, list[str]]:
    """Shortest propagation paths from ``root`` over dependent edges (BFS).

    Returns {node: [root, ..., node]} for every reachable node.  Ties
    between equal-length paths are broken by lexicographic comparison of
    the node-name sequence; the root's own path is just ``[root]``.
    """
    best: dict[str, list[str]] = {root: [root]}
    frontier: dict[str, list[str]] = {root: [root]}
    while frontier:
        nxt: dict[str, list[str]] = {}
        for node, path in frontier.items():
            for dependent in dependents[node]:
                if dependent in best:
                    continue  # already reached at a shorter distance
                candidate = path + [dependent]
                if dependent not in nxt or candidate < nxt[dependent]:
                    nxt[dependent] = candidate
        best.update(nxt)
        frontier = nxt
    return best


def analyze_impacts(before: list[Node], after: list[Node]) -> ImpactResponse:
    """Diff the two graphs and propagate every root cause to its dependents."""
    before_graph, before_contents, _ = validate_graph(before, label="before")
    after_graph, after_contents, _ = validate_graph(after, label="after")

    deleted = sorted(set(before_graph) - set(after_graph))
    causes = _root_causes(before_graph, before_contents, after_graph, after_contents)

    # Reverse edges of the after graph: dependency -> its direct dependents.
    dependents: dict[str, list[str]] = {name: [] for name in after_graph}
    for name, deps in after_graph.items():
        for dep in deps:
            dependents[dep].append(name)

    impacts: dict[str, dict[str, ImpactEntry]] = {}
    for root in sorted(causes):
        for affected, path in _shortest_paths(root, dependents).items():
            impacts.setdefault(affected, {})[root] = ImpactEntry(
                changes=list(causes[root]), path=path
            )

    return ImpactResponse(
        deleted_nodes=deleted,
        root_causes={
            name: RootCause(changes=list(causes[name])) for name in sorted(causes)
        },
        impacts={
            node: {root: entries[root] for root in sorted(entries)}
            for node, entries in sorted(impacts.items())
        },
    )


__all__ = ["analyze_impacts"]
