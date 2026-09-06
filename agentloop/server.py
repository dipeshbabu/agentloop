from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from agentloop.config import get_admin_api_key, get_api_key, get_cors_origins, require_api_key
from agentloop.findings import build_diagnosis
from agentloop.issues import build_issue_drafts
from agentloop.optimizer import build_optimization_plan
from agentloop.quality import QualityValidationError, build_quality_report
from agentloop.schema import TraceValidationError
from agentloop.store import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    FindingNotFoundError,
    FindingTransitionError,
    InvalidCursorError,
    TraceProjectConflictError,
    TraceStore,
    get_store,
)
from agentloop.tracer import AgentTrace
from agentloop.value import build_value_report
from agentloop.version import __version__

app = FastAPI(title="AgentLoop API", version=__version__)

_cors_origins = get_cors_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class TracePayload(BaseModel):
    name: str
    run_id: str
    schema_version: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_ms: float | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class CreateApiKeyPayload(BaseModel):
    project_id: str = "default"
    name: str = "default"


class QualityReportPayload(BaseModel):
    fixtures: list[dict[str, Any]]
    baseline_run_id: str | None = None
    candidate_run_id: str | None = None
    min_score: float | None = Field(default=None, ge=0, le=1)

    @field_validator("min_score", mode="before")
    @classmethod
    def validate_min_score_type(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError("min_score must be a number between 0 and 1")
        return value


def store() -> TraceStore:
    db = get_store()
    db.init()
    return db


def resolve_project(
    x_agentloop_key: str | None = Header(default=None),
    db: TraceStore = Depends(store),
) -> str:
    if x_agentloop_key:
        key_record = db.verify_api_key(x_agentloop_key)
        if key_record is not None:
            return str(key_record["project_id"])

        expected = get_api_key()
        if require_api_key() and expected and hmac.compare_digest(x_agentloop_key, expected):
            return "default"

        raise HTTPException(status_code=401, detail="invalid AgentLoop API key")

    if require_api_key():
        raise HTTPException(status_code=401, detail="missing AgentLoop API key")

    return "default"


def resolve_admin(x_agentloop_admin_key: str | None = Header(default=None)) -> None:
    admin_key = get_admin_api_key()
    if admin_key:
        if x_agentloop_admin_key and hmac.compare_digest(x_agentloop_admin_key, admin_key):
            return
        raise HTTPException(status_code=401, detail="invalid AgentLoop admin API key")

    if require_api_key():
        raise HTTPException(
            status_code=503,
            detail="AGENTLOOP_ADMIN_API_KEY is required to create API keys when API auth is enabled",
        )


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "version": __version__, "auth_required": require_api_key()}


@app.get("/readyz")
def readyz(db: TraceStore = Depends(store)) -> dict[str, str]:
    db.usage_summary(project_id="default")
    return {"status": "ready"}


@app.post("/api-keys")
def create_api_key(
    payload: CreateApiKeyPayload,
    _: None = Depends(resolve_admin),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    return db.create_api_key(project_id=payload.project_id, name=payload.name)


@app.post("/traces")
def ingest_trace(
    payload: TracePayload,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    try:
        trace = AgentTrace.from_dict(payload.model_dump())
    except TraceValidationError as exc:
        raise HTTPException(
            status_code=422, detail={"field": exc.field, "reason": exc.reason}
        ) from None
    try:
        db.save_trace(trace, project_id=project_id)
    except TraceProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"ok": True, "run_id": trace.run_id, "project_id": project_id, "report": trace.report()}


def _selected_project(project_id_filter: str | None, authenticated_project: str) -> str:
    if (
        require_api_key()
        and project_id_filter is not None
        and project_id_filter != authenticated_project
    ):
        raise HTTPException(status_code=403, detail="project access denied")
    return project_id_filter or authenticated_project


@app.get("/traces")
def list_traces(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    page_size: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    """List traces for a project.

    Without `page_size`/`cursor`, returns the full (bounded) list for
    backward compatibility. Pass `page_size` to opt into keyset pagination:
    the response then includes `next_cursor` — pass it back as `cursor` to
    fetch the next page; `next_cursor: null` means there are no more rows.
    """
    selected_project = _selected_project(project_id_filter, project_id)
    if page_size is None and cursor is None:
        return {
            "project_id": selected_project,
            "traces": db.list_traces(project_id=selected_project),
            "next_cursor": None,
        }
    try:
        page = db.list_traces_page(
            project_id=selected_project, limit=page_size or DEFAULT_PAGE_SIZE, cursor=cursor
        )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "project_id": selected_project,
        "traces": page["items"],
        "next_cursor": page["next_cursor"],
    }


def _load_trace_or_404(db: TraceStore, run_id: str, project_id: str) -> AgentTrace:
    trace = db.get_trace(run_id=run_id, project_id=project_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


@app.get("/traces/{run_id}/report")
def get_report(
    run_id: str,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    return _load_trace_or_404(db, run_id, project_id).report()


@app.get("/traces/{run_id}/optimize")
def optimize_trace(
    run_id: str,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    return build_optimization_plan(_load_trace_or_404(db, run_id, project_id))


@app.get(
    "/traces/{run_id}/diagnose",
    deprecated=True,
    description="Read-only compatibility alias for GET /traces/{run_id}/diagnosis. "
    "Available throughout 0.x; use POST /traces/{run_id}/diagnosis to persist findings.",
)
@app.get("/traces/{run_id}/diagnosis")
def diagnose_trace(
    run_id: str,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    """Compute current diagnosis without changing persisted findings."""
    return build_diagnosis(_load_trace_or_404(db, run_id, project_id))


@app.post("/traces/{run_id}/diagnosis")
def persist_trace_diagnosis(
    run_id: str,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    """Recompute and persist findings, preserving existing lifecycle decisions."""
    diagnosis = build_diagnosis(_load_trace_or_404(db, run_id, project_id))
    db.save_diagnosis(diagnosis, project_id=project_id)
    return diagnosis


@app.get("/findings")
def list_findings(
    status: str | None = Query(default=None),
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    page_size: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    """List findings for a project. See `list_traces` for the pagination contract.

    Paginated results are ordered by recency (most recently updated first),
    not by the priority order `optimization-queue` uses.
    """
    selected_project = _selected_project(project_id_filter, project_id)
    if page_size is None and cursor is None:
        return {
            "project_id": selected_project,
            "findings": db.list_findings(project_id=selected_project, status=status),
            "next_cursor": None,
        }
    try:
        page = db.list_findings_page(
            project_id=selected_project,
            status=status,
            limit=page_size or DEFAULT_PAGE_SIZE,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "project_id": selected_project,
        "findings": page["items"],
        "next_cursor": page["next_cursor"],
    }


class FindingStatusPayload(BaseModel):
    status: str


@app.post("/findings/{run_id}/{finding_id}/status")
def update_finding_status(
    run_id: str,
    finding_id: str,
    payload: FindingStatusPayload,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    try:
        return db.update_finding_status(project_id, run_id, finding_id, payload.status)
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except FindingTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/optimization-queue")
def optimization_queue(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = _selected_project(project_id_filter, project_id)
    return {
        "project_id": selected_project,
        "queue": db.optimization_queue(project_id=selected_project),
    }


@app.get("/optimization-queue/github-issues")
def github_issue_drafts(
    limit: int = Query(default=5, ge=1, le=20),
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = _selected_project(project_id_filter, project_id)
    queue = db.optimization_queue(project_id=selected_project)
    return {
        "project_id": selected_project,
        "issue_drafts": build_issue_drafts(queue, limit=limit),
    }


@app.post("/quality-report")
def quality_report_endpoint(
    payload: QualityReportPayload,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    scorer_types = {
        str(fixture["scorer"].get("type", "")).strip().lower()
        for fixture in payload.fixtures
        if isinstance(fixture.get("scorer"), dict)
    }
    if "custom" in scorer_types:
        raise HTTPException(
            status_code=400,
            detail="custom Python scorers are not accepted by the HTTP API",
        )
    baseline_trace = (
        _load_trace_or_404(db, payload.baseline_run_id, project_id)
        if payload.baseline_run_id
        else None
    )
    candidate_trace = (
        _load_trace_or_404(db, payload.candidate_run_id, project_id)
        if payload.candidate_run_id
        else None
    )
    try:
        return build_quality_report(
            payload.fixtures,
            baseline_trace=baseline_trace,
            candidate_trace=candidate_trace,
            min_score=payload.min_score,
        )
    except QualityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/traces/{run_id}/value")
def value_report(
    run_id: str,
    runs_per_month: int = Query(default=1000, ge=0),
    engineer_hourly_rate_usd: float = Query(default=150.0, ge=0),
    incident_cost_usd: float = Query(default=500.0, ge=0),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    trace = _load_trace_or_404(db, run_id, project_id)
    return build_value_report(
        trace,
        runs_per_month=runs_per_month,
        engineer_hourly_rate_usd=engineer_hourly_rate_usd,
        incident_cost_usd=incident_cost_usd,
    )


@app.get("/usage")
def usage_summary(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = _selected_project(project_id_filter, project_id)
    return db.usage_summary(project_id=selected_project)
