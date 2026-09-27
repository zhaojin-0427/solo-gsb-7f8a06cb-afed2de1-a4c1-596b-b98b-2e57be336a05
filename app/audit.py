"""Execution-audit replay for recorded build-event streams.

Given the same graph + previous fingerprints as /plans and a list of
execution events (unique non-negative ``seq``, ``node``, ``type`` —
``start``/``success``/``failure``), the audit:

* recomputes fingerprints with the /plans rules; cache-hit nodes count as
  completed from the very start (terminal state ``cached``);
* replays the events in ascending ``seq`` order over the cache-missing
  nodes, which begin as ``pending``:

  - ``start`` is legal only while the node is pending *and* every direct
    dependency is completed (cached or succeeded) — the node becomes
    ``running``;
  - ``success``/``failure`` are legal only while the node is running
    (started, not yet ended) — the node becomes ``succeeded``/``failed``;

* rejects every illegal event without changing any state and records it
  as a violation with its ``seq``, ``node`` and reason;
* reports the terminal state of every node: ``cached``, ``succeeded``,
  ``failed``, ``running`` or ``pending`` — plus ``blocked`` for unfinished
  nodes downstream of a failed node (a failed dependency never completes,
  so those successors can never start).

Determinism
-----------
``seq`` values are unique, so sorting events by ``seq`` fixes the replay
order regardless of the request payload order; violations are emitted in
that order and the state map is name-sorted.
"""
from __future__ import annotations

from app.models import (
    ExecutionAuditResponse,
    ExecutionEvent,
    Node,
    Violation,
)
from app.planner import PlanError, compute_fingerprints, validate_graph

# Internal replay states; the public ``blocked`` state is derived afterwards.
CACHED = "cached"
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

COMPLETED = (CACHED, SUCCEEDED)

REASON_DEPENDENCIES_INCOMPLETE = "dependencies_incomplete"
REASON_NOT_PENDING = "not_pending"
REASON_NOT_RUNNING = "not_running"


def _validate_events(
    events: list[ExecutionEvent], graph: dict[str, list[str]]
) -> None:
    """Reject duplicate seq values and references to unknown nodes."""
    seen: set[int] = set()
    duplicates: set[int] = set()
    for event in events:
        if event.seq in seen:
            duplicates.add(event.seq)
        seen.add(event.seq)
    if duplicates:
        raise PlanError(
            "duplicate_seq",
            "Event seq values must be unique",
            {"seq": sorted(duplicates)},
        )
    unknown = sorted({event.node for event in events if event.node not in graph})
    if unknown:
        raise PlanError(
            "unknown_node",
            "Events reference nodes that are not in the graph",
            {"names": unknown},
        )


def audit_execution(
    nodes: list[Node],
    previous: dict[str, str],
    events: list[ExecutionEvent],
) -> ExecutionAuditResponse:
    """Replay ``events`` over the graph and audit them against the rules."""
    graph, contents, order = validate_graph(nodes)
    _validate_events(events, graph)

    # ---- cache hits count as completed from the start -------------------
    fingerprints = compute_fingerprints(graph, contents, order)
    state = {
        name: CACHED if fingerprints[name] == previous.get(name) else PENDING
        for name in graph
    }

    # ---- replay in ascending seq order ----------------------------------
    violations: list[Violation] = []
    for event in sorted(events, key=lambda e: e.seq):
        reason: str | None = None
        if event.type == "start":
            if state[event.node] != PENDING:
                reason = REASON_NOT_PENDING
            elif any(state[dep] not in COMPLETED for dep in graph[event.node]):
                reason = REASON_DEPENDENCIES_INCOMPLETE
            else:
                state[event.node] = RUNNING
        elif state[event.node] != RUNNING:
            reason = REASON_NOT_RUNNING
        else:
            state[event.node] = SUCCEEDED if event.type == "success" else FAILED
        if reason is not None:
            violations.append(
                Violation(
                    seq=event.seq, node=event.node, type=event.type, reason=reason
                )
            )

    # ---- blocked: unfinished nodes downstream of a failed node ----------
    blocked: set[str] = set()
    for name in order:  # topological: dependencies before their dependents
        if state[name] != PENDING:
            continue
        if any(state[dep] == FAILED or dep in blocked for dep in graph[name]):
            blocked.add(name)

    states = {
        name: ("blocked" if name in blocked else state[name])
        for name in sorted(graph)
    }
    return ExecutionAuditResponse(states=states, violations=violations)


__all__ = ["audit_execution"]
