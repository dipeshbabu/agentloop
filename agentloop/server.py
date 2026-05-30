from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agentloop.config import get_admin_api_key, get_api_key, get_cors_origins, require_api_key
from agentloop.findings import build_diagnosis
from agentloop.issues import build_issue_drafts
from agentloop.optimizer import build_optimization_plan
from agentloop.store import TraceStore, get_store
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
    started_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class CreateApiKeyPayload(BaseModel):
    project_id: str = "default"
    name: str = "default"


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
        if require_api_key() and expected and x_agentloop_key == expected:
            return "default"

        raise HTTPException(status_code=401, detail="invalid AgentLoop API key")

    if require_api_key():
        raise HTTPException(status_code=401, detail="missing AgentLoop API key")

    return "default"


def resolve_admin(x_agentloop_admin_key: str | None = Header(default=None)) -> None:
    admin_key = get_admin_api_key()
    if admin_key:
        if x_agentloop_admin_key == admin_key:
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
    trace = AgentTrace.from_dict(payload.model_dump())
    db.save_trace(trace, project_id=project_id)
    return {"ok": True, "run_id": trace.run_id, "project_id": project_id, "report": trace.report()}


@app.get("/traces")
def list_traces(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = project_id_filter or project_id
    return {"project_id": selected_project, "traces": db.list_traces(project_id=selected_project)}


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


@app.get("/traces/{run_id}/diagnose")
def diagnose_trace(
    run_id: str,
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    diagnosis = build_diagnosis(_load_trace_or_404(db, run_id, project_id))
    db.save_diagnosis(diagnosis, project_id=project_id)
    return diagnosis


@app.get("/findings")
def list_findings(
    status: str | None = Query(default=None),
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = project_id_filter or project_id
    return {
        "project_id": selected_project,
        "findings": db.list_findings(project_id=selected_project, status=status),
    }


@app.get("/optimization-queue")
def optimization_queue(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = project_id_filter or project_id
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
    selected_project = project_id_filter or project_id
    queue = db.optimization_queue(project_id=selected_project)
    return {
        "project_id": selected_project,
        "issue_drafts": build_issue_drafts(queue, limit=limit),
    }


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
    selected_project = project_id_filter or project_id
    return db.usage_summary(project_id=selected_project)
