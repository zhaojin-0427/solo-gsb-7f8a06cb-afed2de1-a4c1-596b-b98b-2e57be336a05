"""Pydantic models for the build plan API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, field_validator


class Node(BaseModel):
    """A single build graph node supplied by the client."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Unique node name.")
    content_digest: str = Field(
        ...,
        min_length=1,
        description="Opaque summary/hash of the node's own content.",
    )
    dependency_names: list[str] = Field(
        default_factory=list,
        description="Names of direct dependencies (directed edges).",
    )

    @field_validator("dependency_names")
    @classmethod
    def _validate_dependencies(cls, value: list[str]) -> list[str]:
        for dep in value:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError("dependency names must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate dependency names: {value}")
        return value


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(..., description="Build graph nodes.")
    previous_fingerprints: dict[str, str] = Field(
        default_factory=dict,
        description="Node name -> fingerprint from the previous build.",
    )


class PlanNode(BaseModel):
    fingerprint: str = Field(..., description="Computed SHA-256 fingerprint.")
    cache_hit: bool = Field(..., description="True when it matches the previous fingerprint.")
    batch: Optional[int] = Field(
        ...,
        description="0-based parallel build batch; null when the node is a cache hit."
    )
    dependencies: list[str] = Field(
        ..., description="Direct dependency names, sorted (echoed for clarity)."
    )


class PlanResponse(BaseModel):
    plans: dict[str, PlanNode] = Field(
        ..., description="Per-node result keyed by node name."
    )
    batches: list[list[str]] = Field(
        ...,
        description="Cache-missing nodes grouped into parallel build batches.",
    )


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


# ------------------------------------------------------------------ impacts

ChangeType = Literal["added", "content_changed", "dependencies_changed"]


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: list[Node] = Field(..., description="Nodes of the previous graph snapshot.")
    after: list[Node] = Field(..., description="Nodes of the new graph snapshot.")


class RootCause(BaseModel):
    changes: list[ChangeType] = Field(
        ..., description="Change kinds detected for this root-cause node."
    )


class ImpactEntry(BaseModel):
    changes: list[ChangeType] = Field(
        ..., description="Change kinds of the root cause reaching this node."
    )
    path: list[str] = Field(
        ...,
        description=(
            "Shortest propagation path from the root cause to this node; "
            "equal-length ties are broken by node-name sequence order."
        ),
    )


class ImpactResponse(BaseModel):
    deleted_nodes: list[str] = Field(
        ..., description="Nodes present in `before` but absent from `after`, sorted."
    )
    root_causes: dict[str, RootCause] = Field(
        ..., description="Root-cause nodes of the new graph, keyed by node name."
    )
    impacts: dict[str, dict[str, ImpactEntry]] = Field(
        ...,
        description=(
            "Affected node name -> root cause name -> change kinds and "
            "shortest propagation path."
        ),
    )


# ---------------------------------------------------------- cache retention


class Artifact(BaseModel):
    """A cached build artifact offered for retention."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Node the artifact belongs to.")
    fingerprint: str = Field(
        ...,
        min_length=1,
        description="Fingerprint recorded when the artifact was built.",
    )
    size: PositiveInt = Field(
        ..., description="Storage units occupied by the artifact (positive integer)."
    )


class CacheRetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    target: str = Field(
        ..., min_length=1, description="Node that must be available after the build."
    )
    rebuild_costs: dict[str, NonNegativeInt] = Field(
        ...,
        description="Node name -> non-negative integer cost of rebuilding that node.",
    )
    artifacts: list[Artifact] = Field(
        default_factory=list,
        description="Existing cached artifacts (node name, fingerprint, size).",
    )
    capacity: NonNegativeInt = Field(
        ..., description="Total size budget for the kept artifacts (non-negative)."
    )


class CacheRetentionResponse(BaseModel):
    kept: list[str] = Field(
        ..., description="Names of the retained artifacts, sorted."
    )
    evicted: list[str] = Field(
        ...,
        description=(
            "Names of the artifacts not retained (stale fingerprint or not "
            "selected), sorted."
        ),
    )
    rebuild: list[str] = Field(
        ...,
        description=(
            "Nodes that must be rebuilt so the target becomes available "
            "(shared upstream counted once), sorted."
        ),
    )
    total_cost: NonNegativeInt = Field(
        ..., description="Sum of rebuild costs over `rebuild`."
    )


# ---------------------------------------------------------- execution audit

EventType = Literal["start", "success", "failure"]

NodeState = Literal["cached", "succeeded", "failed", "running", "pending", "blocked"]

ViolationReason = Literal["dependencies_incomplete", "not_pending", "not_running"]


class ExecutionEvent(BaseModel):
    """A single recorded build-execution event."""

    model_config = ConfigDict(extra="forbid")

    seq: NonNegativeInt = Field(
        ..., description="Unique non-negative sequence number (replay order)."
    )
    node: str = Field(..., min_length=1, description="Node the event refers to.")
    type: EventType = Field(
        ..., description="Event kind: start, success or failure."
    )


class ExecutionAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    previous_fingerprints: dict[str, str] = Field(
        default_factory=dict,
        description="Node name -> fingerprint from the previous build.",
    )
    events: list[ExecutionEvent] = Field(
        default_factory=list,
        description="Recorded execution events, replayed in ascending seq order.",
    )


class Violation(BaseModel):
    """An event that broke the execution rules and was ignored."""

    seq: NonNegativeInt = Field(..., description="Sequence number of the event.")
    node: str = Field(..., description="Node the event referred to.")
    type: EventType = Field(..., description="Event kind (echoed for clarity).")
    reason: ViolationReason = Field(..., description="Why the event was rejected.")


class ExecutionAuditResponse(BaseModel):
    states: dict[str, NodeState] = Field(
        ..., description="Terminal state per node, keyed by node name."
    )
    violations: list[Violation] = Field(
        ...,
        description="Events that were rejected and ignored, in ascending seq order.",
    )


# --------------------------------------------------------- failure isolation


class FailureIsolationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    failed_nodes: list[str] = Field(
        ...,
        min_length=1,
        description="Non-empty list of failed build node names.",
    )
    targets: list[str] = Field(
        ...,
        min_length=1,
        description="Non-empty list of release target node names.",
    )
    isolation_costs: dict[str, NonNegativeInt] = Field(
        ...,
        description=(
            "Isolatable node name -> non-negative integer cost of isolating "
            "(removing) that node. Failed nodes and targets must not appear here."
        ),
    )


class FailureIsolationResponse(BaseModel):
    isolated: list[str] = Field(
        ...,
        description=(
            "Nodes to isolate so that every failed-node -> target path is "
            "cut (sorted). Empty when no such path exists in the first place."
        ),
    )
    total_cost: NonNegativeInt = Field(
        ..., description="Sum of the isolation costs over `isolated`."
    )


# ---------------------------------------------------------- release bundles


class BundleArtifact(BaseModel):
    """A selectable build artifact offered for a release bundle."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Unique artifact id.")
    node: str = Field(
        ..., min_length=1, description="Graph node the artifact was built for."
    )
    fingerprint: str = Field(
        ...,
        min_length=1,
        description="Fingerprint recorded when the artifact was built.",
    )
    size: PositiveInt = Field(
        ..., description="Storage units occupied by the artifact (positive integer)."
    )
    dependencies: dict[str, list[str]] = Field(
        ...,
        description=(
            "Direct dependency node name -> non-empty list of acceptable "
            "artifact ids for that dependency."
        ),
    )

    @field_validator("dependencies")
    @classmethod
    def _validate_dependency_lists(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        for node_name, acceptable in value.items():
            if not isinstance(node_name, str) or not node_name.strip():
                raise ValueError("dependency node names must be non-empty strings")
            if not isinstance(acceptable, list) or not acceptable:
                raise ValueError(
                    "each dependency must map to a non-empty list of artifact ids"
                )
            for artifact_id in acceptable:
                if not isinstance(artifact_id, str) or not artifact_id.strip():
                    raise ValueError("artifact ids must be non-empty strings")
        return value


class ReleaseBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    target: str = Field(
        ..., min_length=1, description="Release target node (must be in the graph)."
    )
    artifacts: list[BundleArtifact] = Field(
        ...,
        description=(
            "Offered artifacts (at most 16); only artifacts whose fingerprint "
            "matches the node recomputation are selectable."
        ),
    )


class ReleaseBundleResponse(BaseModel):
    ready: bool = Field(
        ...,
        description="True when a consistent bundle covering the target and all transitive dependencies exists.",
    )
    artifact_ids: list[str] = Field(
        ...,
        description=(
            "Selected artifact ids, sorted (one per closure node); empty when "
            "not ready."
        ),
    )
    total_size: NonNegativeInt = Field(
        ..., description="Sum of the selected artifact sizes (0 when not ready)."
    )


# --------------------------------------------------------- release transitions


class ReleaseTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    target: str = Field(
        ..., min_length=1, description="Release target node (must be in the graph)."
    )
    artifacts: list[BundleArtifact] = Field(
        ...,
        description=(
            "Offered artifacts; each must belong to a closure node, and each "
            "closure node must offer 1..3 artifacts. Fingerprints are "
            "metadata only and are not recomputed."
        ),
    )
    current_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Artifact ids of the current bundle (exactly one per closure node).",
    )
    next_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Artifact ids of the desired next bundle (exactly one per closure node).",
    )

    @field_validator("current_ids", "next_ids")
    @classmethod
    def _validate_endpoint_ids(cls, value: list[str]) -> list[str]:
        for artifact_id in value:
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("endpoint artifact ids must be non-empty strings")
        return value


class TransitionStep(BaseModel):
    node: str = Field(
        ..., description="Closure node whose artifact is replaced by this step."
    )
    id: str = Field(
        ..., description="Artifact id chosen for `node` after the switch."
    )
    bundle: dict[str, str] = Field(
        ...,
        description=(
            "Full node name -> artifact id mapping of the closure after the "
            "switch (exactly one id per closure node)."
        ),
    )


class ReleaseTransitionResponse(BaseModel):
    possible: bool = Field(
        ...,
        description=(
            "True when a dependency-consistent sequence of single-node "
            "switches turns the current bundle into the next bundle."
        ),
    )
    steps: list[TransitionStep] = Field(
        ...,
        description=(
            "Minimum-length switch sequence; among shortest sequences the "
            "lexicographically smallest sequence of (node, id) pairs. "
            "Empty when the bundles are identical or when no sequence exists."
        ),
    )


# ---------------------------------------------------------- release policies

EdgeType = Literal["bundled", "build_only"]


class ReleasePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(
        ..., description="Build graph nodes (same schema as /plans)."
    )
    target: str = Field(
        ..., min_length=1, description="Release target node (must be in the graph)."
    )
    edge_types: dict[str, dict[str, EdgeType]] = Field(
        ...,
        description=(
            "Node name -> direct dependency name -> edge type.  Every graph "
            "node must appear exactly once and each map must list exactly "
            "the node's direct dependencies."
        ),
    )
    labels: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Node name -> organization clause tags carried by that node.",
    )
    fulfilled: list[str] = Field(
        default_factory=list,
        description="Clause tags the organization has already fulfilled.",
    )
    exclusive_pairs: list[list[str]] = Field(
        default_factory=list,
        description="Two-tag combinations that must never appear together.",
    )

    @field_validator("edge_types")
    @classmethod
    def _validate_edge_types(
        cls, value: dict[str, dict[str, EdgeType]]
    ) -> dict[str, dict[str, EdgeType]]:
        for node_name, deps in value.items():
            if not isinstance(node_name, str) or not node_name.strip():
                raise ValueError("edge-type node names must be non-empty strings")
            for dep in deps:
                if not isinstance(dep, str) or not dep.strip():
                    raise ValueError(
                        "edge-type dependency names must be non-empty strings"
                    )
        return value

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for node_name, tags in value.items():
            if not isinstance(node_name, str) or not node_name.strip():
                raise ValueError("label node names must be non-empty strings")
            for tag in tags:
                if not isinstance(tag, str) or not tag.strip():
                    raise ValueError("clause tags must be non-empty strings")
        return value

    @field_validator("fulfilled")
    @classmethod
    def _validate_fulfilled(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError("fulfilled tags must be non-empty strings")
        return value

    @field_validator("exclusive_pairs")
    @classmethod
    def _validate_exclusive_pairs(cls, value: list[list[str]]) -> list[list[str]]:
        for pair in value:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(
                    "each exclusive pair must contain exactly two tags"
                )
            for tag in pair:
                if not isinstance(tag, str) or not tag.strip():
                    raise ValueError("clause tags must be non-empty strings")
        return value


class MissingTag(BaseModel):
    """A clause tag that appears in the bundled closure but is unfulfilled."""

    tag: str = Field(..., description="Clause tag missing fulfillment.")
    path: list[str] = Field(
        ...,
        description=(
            "Shortest bundled-edge path from the target to a node carrying "
            "the tag; equal-length ties broken by node-name sequence order."
        ),
    )


class TagConflict(BaseModel):
    """An exclusive pair whose tags both appear in the bundled closure."""

    pair: list[str] = Field(
        ..., description="The conflicting exclusive pair, sorted."
    )
    paths: dict[str, list[str]] = Field(
        ...,
        description=(
            "Each tag of the pair -> shortest bundled-edge path from the "
            "target to a node carrying it."
        ),
    )


class ReleasePolicyResponse(BaseModel):
    allowed: bool = Field(
        ...,
        description="True when no tag is missing and no exclusive pair conflicts.",
    )
    missing: list[MissingTag] = Field(
        ...,
        description="Unfulfilled clause tags in the bundled closure, sorted by tag.",
    )
    conflicts: list[TagConflict] = Field(
        ...,
        description=(
            "Exclusive pairs whose tags both appear in the bundled closure, "
            "sorted by pair."
        ),
    )
