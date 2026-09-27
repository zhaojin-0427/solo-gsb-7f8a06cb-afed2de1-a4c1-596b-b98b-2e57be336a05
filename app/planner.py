"""Core incremental-build planning logic.

Fingerprint
-----------
For each node *n*, in dependency (topological) order:

    fingerprint(n) = sha256(canonical_json([
        n.content_digest,
        [[dep_name, fingerprint(dep)] for dep in sorted(n.dependency_names)],
    ])).hexdigest()

The dependency list is sorted by name, and dependency fingerprints are always
included (whether or not the dependency is a cache hit), so a change anywhere
in the transitive closure changes the fingerprint of every downstream node.

Batches
-------
Only cache-missing nodes are scheduled.  Dependencies on cache-hit nodes impose
no wait (hit nodes already exist), therefore the batch index of a missing node
is::

    level(n) = 1 + max(level(d) for d in dependencies(n) if d is a miss)
               or 0 if all dependencies are hits / it has no dependencies

All missing nodes with the same level can be built in parallel, and every
level-(k-1) missing node is finished before level k starts.

Determinism
-----------
Inputs are processed through name-sorted structures at every step; reordering
the request payload never changes the result.
"""
from __future__ import annotations

import hashlib
import heapq
import json

from app.models import Node, PlanNode, PlanResponse


class PlanError(Exception):
    """Semantic error in a plan request; surfaced as HTTP 400."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _canonical_payload(content_digest: str, deps: list[tuple[str, str]]) -> bytes:
    """Canonical JSON bytes for a node's fingerprint input.

    ``json.dumps`` with sort_keys/separators and a fixed encoding gives a
    stable, unambiguous serialization.
    """
    body = [content_digest, [[name, fp] for name, fp in deps]]
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _fingerprint(content_digest: str, deps: list[tuple[str, str]]) -> str:
    return hashlib.sha256(_canonical_payload(content_digest, deps)).hexdigest()


def _find_cycle(graph: dict[str, list[str]]) -> list[str]:
    """Return one concrete cycle as an ordered path (first == last).

    Classic three-colour iterative DFS; an edge to a GRAY (on-path) node
    closes a cycle.  Returns [] when the graph is acyclic.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in graph}

    for root in sorted(graph):
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        path: list[str] = [root]
        iterators = [iter(sorted(graph[root]))]
        while path:
            neighbor = next(iterators[-1], None)
            if neighbor is None:
                color[path.pop()] = BLACK
                iterators.pop()
                continue
            if color[neighbor] == WHITE:
                color[neighbor] = GRAY
                path.append(neighbor)
                iterators.append(iter(sorted(graph[neighbor])))
            elif color[neighbor] == GRAY:
                start = path.index(neighbor)
                return path[start:] + [neighbor]
    return []


def topological_order(graph: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm with a name heap, yielding a deterministic order.

    Raises PlanError("cycle_detected") when the graph contains a cycle.
    """
    indegree = {name: 0 for name in graph}
    for name, deps in graph.items():
        for dep in deps:
            indegree[name] += 1

    heap: list[str] = [name for name, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)

    order: list[str] = []
    dependents: dict[str, list[str]] = {name: [] for name in graph}
    for name, deps in graph.items():
        for dep in deps:
            dependents[dep].append(name)

    while heap:
        name = heapq.heappop(heap)
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, dependent)

    if len(order) != len(graph):
        cycle = _find_cycle(graph)
        raise PlanError(
            "cycle_detected",
            "Dependency graph contains a cycle",
            {"cycle": cycle},
        )
    return order


def validate_graph(
    nodes: list[Node], label: str | None = None
) -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    """Validate nodes, returning (graph, contents, topological order).

    Checks duplicate names, missing dependencies and cycles.  When ``label``
    is given it is added to error details as ``graph`` so callers handling
    several graphs (e.g. /impacts) can tell which one failed.
    """
    def _details(base: dict) -> dict:
        return {**base, "graph": label} if label is not None else base

    # ---- validation: unique names --------------------------------------
    graph: dict[str, list[str]] = {}
    contents: dict[str, str] = {}
    for node in nodes:
        if node.name in graph:
            raise PlanError(
                "duplicate_node",
                f"Duplicate node name: {node.name!r}",
                _details({"name": node.name}),
            )
        graph[node.name] = list(node.dependency_names)
        contents[node.name] = node.content_digest

    # ---- validation: every dependency exists ----------------------------
    missing: dict[str, list[str]] = {}
    for name in sorted(graph):
        unknown = sorted({d for d in graph[name] if d not in graph})
        if unknown:
            missing[name] = unknown
    if missing:
        raise PlanError(
            "missing_dependency",
            "One or more nodes depend on names that are not in the graph",
            _details({"missing": missing}),
        )

    # ---- acyclicity (also yields the topological order) -----------------
    try:
        order = topological_order(graph)
    except PlanError as exc:
        raise PlanError(exc.code, exc.message, _details(exc.details)) from exc
    return graph, contents, order


def compute_fingerprints(
    graph: dict[str, list[str]],
    contents: dict[str, str],
    order: list[str],
) -> dict[str, str]:
    """Content-addressable fingerprint per node, computed in topological order."""
    fingerprints: dict[str, str] = {}
    for name in order:
        deps = sorted(
            ((dep, fingerprints[dep]) for dep in graph[name]),
            key=lambda pair: pair[0],
        )
        fingerprints[name] = _fingerprint(contents[name], deps)
    return fingerprints


def build_plan(nodes: list[Node], previous: dict[str, str]) -> PlanResponse:
    """Validate the graph, compute fingerprints and schedule miss batches."""
    graph, contents, order = validate_graph(nodes)
    # ---- fingerprints (in topological order) ----------------------------
    fingerprints = compute_fingerprints(graph, contents, order)

    # ---- cache hits -----------------------------------------------------
    hits = {name for name in graph if fingerprints[name] == previous.get(name)}

    # ---- batch levels over missing nodes only (iterative, topo order) ---
    levels: dict[str, int] = {}
    for name in order:
        if name in hits:
            continue
        upstream = [levels[d] for d in graph[name] if d not in hits]
        levels[name] = 1 + max(upstream) if upstream else 0

    batches: list[list[str]] = [[] for _ in range(
        (max(levels.values()) + 1) if levels else 0
    )]
    for name, level in levels.items():
        batches[level].append(name)
    for batch in batches:
        batch.sort()

    plans = {
        name: PlanNode(
            fingerprint=fingerprints[name],
            cache_hit=name in hits,
            batch=None if name in hits else levels[name],
            dependencies=sorted(graph[name]),
        )
        for name in sorted(graph)
    }
    return PlanResponse(plans=plans, batches=batches)


# Re-exported for potential diagnostics/tests.
__all__ = [
    "PlanError",
    "build_plan",
    "compute_fingerprints",
    "topological_order",
    "validate_graph",
]
