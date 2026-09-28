"""FastAPI application exposing the incremental build planner."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import audit_execution
from app.bundles import plan_release_bundle
from app.impacts import analyze_impacts
from app.isolation import plan_failure_isolation
from app.models import (
    CacheRetentionRequest,
    CacheRetentionResponse,
    ErrorBody,
    ExecutionAuditRequest,
    ExecutionAuditResponse,
    FailureIsolationRequest,
    FailureIsolationResponse,
    ImpactRequest,
    ImpactResponse,
    PlanRequest,
    PlanResponse,
    ReleaseBundleRequest,
    ReleaseBundleResponse,
    ReleasePolicyRequest,
    ReleasePolicyResponse,
    ReleaseTransitionRequest,
    ReleaseTransitionResponse,
)
from app.planner import PlanError, build_plan
from app.policies import evaluate_release_policy
from app.retention import plan_cache_retention
from app.transitions import plan_release_transitions

app = FastAPI(
    title="Incremental Build Planner",
    version="1.7.0",
    description=(
        "Computes content-addressable fingerprints over a dependency graph "
        "and schedules cache-missing nodes into parallel build batches. "
        "Also diffs two graph snapshots and reports change impacts, "
        "selects which cached artifacts to keep under a capacity limit so "
        "the target node's rebuild cost is minimized, replays recorded "
        "build-execution events to audit them against the cache rules, "
        "chooses a minimum-cost set of nodes to isolate before release so "
        "that no failed build node can reach a release target, picks "
        "the smallest dependency-consistent artifact bundle for a release, "
        "plans the minimum number of dependency-consistent artifact "
        "switches that turn one release bundle into another, and evaluates "
        "organization clause tags over the bundled dependency closure to "
        "decide whether a target may be released."
    ),
)


@app.exception_handler(PlanError)
async def plan_error_handler(_request: Request, exc: PlanError) -> JSONResponse:
    body = ErrorBody(code=exc.code, message=exc.message, details=exc.details)
    return JSONResponse(status_code=400, content=body.model_dump())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse, status_code=201)
async def create_plan(request: PlanRequest) -> PlanResponse:
    return build_plan(request.nodes, request.previous_fingerprints)


@app.post("/impacts", response_model=ImpactResponse)
async def create_impacts(request: ImpactRequest) -> ImpactResponse:
    return analyze_impacts(request.before, request.after)


@app.post("/cache-retention", response_model=CacheRetentionResponse)
async def create_cache_retention(
    request: CacheRetentionRequest,
) -> CacheRetentionResponse:
    return plan_cache_retention(
        request.nodes,
        request.target,
        request.rebuild_costs,
        request.artifacts,
        request.capacity,
    )


@app.post("/execution-audits", response_model=ExecutionAuditResponse)
async def create_execution_audit(
    request: ExecutionAuditRequest,
) -> ExecutionAuditResponse:
    return audit_execution(
        request.nodes,
        request.previous_fingerprints,
        request.events,
    )


@app.post("/failure-isolation", response_model=FailureIsolationResponse)
async def create_failure_isolation(
    request: FailureIsolationRequest,
) -> FailureIsolationResponse:
    return plan_failure_isolation(
        request.nodes,
        request.failed_nodes,
        request.targets,
        request.isolation_costs,
    )


@app.post("/release-bundles", response_model=ReleaseBundleResponse)
async def create_release_bundle(
    request: ReleaseBundleRequest,
) -> ReleaseBundleResponse:
    return plan_release_bundle(request.nodes, request.target, request.artifacts)


@app.post("/release-transitions", response_model=ReleaseTransitionResponse)
async def create_release_transition(
    request: ReleaseTransitionRequest,
) -> ReleaseTransitionResponse:
    return plan_release_transitions(
        request.nodes,
        request.target,
        request.artifacts,
        request.current_ids,
        request.next_ids,
    )


@app.post("/release-policies", response_model=ReleasePolicyResponse)
async def create_release_policy(
    request: ReleasePolicyRequest,
) -> ReleasePolicyResponse:
    return evaluate_release_policy(
        request.nodes,
        request.target,
        request.edge_types,
        request.labels,
        request.fulfilled,
        request.exclusive_pairs,
    )
