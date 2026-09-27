"""Organization release-policy evaluation over typed dependency edges.

Given the current build graph (same schema as /plans), a release *target*
node, a ``bundled`` / ``build_only`` type for every direct dependency edge,
the organization clause tags carried by each node, the tags the
organization has already fulfilled and a list of mutually exclusive tag
pairs, decide whether the target may be released.

Policy model
------------
* Only **bundled** edges are followed: the release ships the target and its
  transitive closure over bundled edges alone.  ``build_only`` dependencies
  are needed to build but are not shipped, so their clause tags (and
  everything only reachable through them) never apply.
* Every clause tag carried by a node inside the bundled closure must be
  fulfilled; each unfulfilled tag blocks the release.
* Both tags of an exclusive pair appearing anywhere inside the closure
  (even on the same node) block the release as a conflict, no matter
  whether they are fulfilled.

Reporting
---------
Every missing tag and both ends of every conflicting pair carry the
shortest dependency path from the target to a node carrying the tag (over
bundled edges).  Equal-length ties are broken by lexicographic order of the
node-name sequence; a tag carried by the target itself reports the
single-node path ``[target]``.

Validation
----------
Beyond the usual graph checks (duplicate node, missing dependency, cycle):
unknown target, edge-type maps that do not mirror every node's direct
dependencies exactly, labels referencing unknown nodes, duplicate tags
within a node's label list, duplicate fulfilled tags and malformed
exclusive pairs (self-pairs or pairs listed twice) all produce structured
HTTP 400 errors.

Determinism
-----------
All validation and evaluation goes through name-sorted structures, so the
request payload order never changes the result.
"""
from __future__ import annotations

from app.models import (
    EdgeType,
    MissingTag,
    Node,
    ReleasePolicyResponse,
    TagConflict,
)
from app.planner import PlanError, validate_graph


def _duplicates(values: list[str]) -> list[str]:
    """Sorted list of the values occurring more than once."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return sorted(dupes)


def _shortest_paths(
    target: str, bundled: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Shortest paths from ``target`` over bundled edges (BFS).

    Returns {node: [target, ..., node]} for every node in the bundled
    closure.  Equal-length ties are broken by lexicographic order of the
    node-name sequence; the target's own path is just ``[target]``.
    """
    best: dict[str, list[str]] = {target: [target]}
    frontier: dict[str, list[str]] = {target: [target]}
    while frontier:
        nxt: dict[str, list[str]] = {}
        for node, path in frontier.items():
            for dep in bundled[node]:
                if dep in best:
                    continue  # already reached at a shorter distance
                candidate = path + [dep]
                if dep not in nxt or candidate < nxt[dep]:
                    nxt[dep] = candidate
        best.update(nxt)
        frontier = nxt
    return best


def _validate_edge_types(
    graph: dict[str, list[str]],
    edge_types: dict[str, dict[str, EdgeType]],
) -> None:
    """Edge types must mirror the graph's direct dependencies exactly."""
    missing_nodes = sorted(set(graph) - set(edge_types))
    unexpected_nodes = sorted(set(edge_types) - set(graph))
    bad_edges: list[dict[str, object]] = []
    for name in sorted(set(graph) & set(edge_types)):
        missing = sorted(set(graph[name]) - set(edge_types[name]))
        unexpected = sorted(set(edge_types[name]) - set(graph[name]))
        if missing or unexpected:
            entry: dict[str, object] = {"node": name}
            if missing:
                entry["missing"] = missing
            if unexpected:
                entry["unexpected"] = unexpected
            bad_edges.append(entry)
    if missing_nodes or unexpected_nodes or bad_edges:
        details: dict[str, object] = {}
        if missing_nodes:
            details["missing_nodes"] = missing_nodes
        if unexpected_nodes:
            details["unexpected_nodes"] = unexpected_nodes
        if bad_edges:
            details["edges"] = bad_edges
        raise PlanError(
            "invalid_edge_types",
            "Edge types must list exactly the direct dependencies of every node",
            details,
        )


def _normalize_pairs(exclusive_pairs: list[list[str]]) -> list[tuple[str, str]]:
    """Sorted, de-duplicated ``(low, high)`` pairs; malformed ones raise."""
    self_pairs: set[tuple[str, str]] = set()
    dupe_pairs: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for pair in exclusive_pairs:
        key = (pair[0], pair[1]) if pair[0] <= pair[1] else (pair[1], pair[0])
        if key[0] == key[1]:
            self_pairs.add(key)
        if key in seen:
            dupe_pairs.add(key)
        seen.add(key)
    if self_pairs or dupe_pairs:
        details: dict[str, object] = {}
        if self_pairs:
            details["self_pairs"] = [list(pair) for pair in sorted(self_pairs)]
        if dupe_pairs:
            details["duplicate_pairs"] = [
                list(pair) for pair in sorted(dupe_pairs)
            ]
        raise PlanError(
            "invalid_exclusive_pair",
            "Exclusive pairs must combine two distinct tags and must not repeat",
            details,
        )
    return sorted(seen)


def evaluate_release_policy(
    nodes: list[Node],
    target: str,
    edge_types: dict[str, dict[str, EdgeType]],
    labels: dict[str, list[str]],
    fulfilled: list[str],
    exclusive_pairs: list[list[str]],
) -> ReleasePolicyResponse:
    """Decide whether ``target`` satisfies the organization's release policy."""
    graph, _contents, _order = validate_graph(nodes)

    # ---- validation: target resolves ---------------------------------------
    if target not in graph:
        raise PlanError(
            "unknown_target",
            f"Target node is not in the graph: {target!r}",
            {"target": target},
        )

    # ---- validation: edge types mirror the direct dependencies -------------
    _validate_edge_types(graph, edge_types)

    # ---- validation: per-node tags are unique and reference real nodes -----
    duplicated: dict[str, list[str]] = {}
    for name in sorted(labels):
        dupes = _duplicates(labels[name])
        if dupes:
            duplicated[name] = dupes
    if duplicated:
        raise PlanError(
            "duplicate_label",
            "A node's clause tags must be unique",
            {"labels": duplicated},
        )
    unknown_label_nodes = sorted(set(labels) - set(graph))
    if unknown_label_nodes:
        raise PlanError(
            "unknown_label_node",
            "Labels reference nodes that are not in the graph",
            {"names": unknown_label_nodes},
        )

    # ---- validation: fulfilled tags are unique ------------------------------
    duplicate_fulfilled = _duplicates(fulfilled)
    if duplicate_fulfilled:
        raise PlanError(
            "duplicate_fulfilled",
            "Fulfilled tags must be unique",
            {"tags": duplicate_fulfilled},
        )

    # ---- validation: exclusive pairs are well-formed ------------------------
    pairs = _normalize_pairs(exclusive_pairs)

    # ---- bundled closure: shortest paths from the target --------------------
    bundled = {
        name: sorted(
            dep for dep in graph[name] if edge_types[name][dep] == "bundled"
        )
        for name in graph
    }
    paths = _shortest_paths(target, bundled)

    # ---- clause tags carried inside the closure -----------------------------
    carriers: dict[str, set[str]] = {}
    for name in paths:
        for tag in labels.get(name, ()):
            carriers.setdefault(tag, set()).add(name)

    def _best_path(tag: str) -> list[str]:
        return min(
            (paths[carrier] for carrier in carriers[tag]),
            key=lambda path: (len(path), path),
        )

    fulfilled_set = set(fulfilled)
    missing = [
        MissingTag(tag=tag, path=_best_path(tag))
        for tag in sorted(carriers)
        if tag not in fulfilled_set
    ]
    conflicts = [
        TagConflict(pair=[a, b], paths={a: _best_path(a), b: _best_path(b)})
        for a, b in pairs
        if a in carriers and b in carriers
    ]
    return ReleasePolicyResponse(
        allowed=not missing and not conflicts,
        missing=missing,
        conflicts=conflicts,
    )


__all__ = ["evaluate_release_policy"]
