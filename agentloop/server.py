from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from agentloop.config import get_api_key, require_api_key
from agentloop.optimizer import build_optimization_plan
from agentloop.store import TraceStore, get_store
from agentloop.tracer import AgentTrace

app = FastAPI(title="AgentLoop API", version="0.4.0")


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
        if key_record is None:
            raise HTTPException(status_code=401, detail="invalid AgentLoop API key")
        return str(key_record["project_id"])

    if require_api_key():
        expected = get_api_key()
        if expected and x_agentloop_key == expected:
            return "default"
        raise HTTPException(status_code=401, detail="missing or invalid AgentLoop API key")

    return "default"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api-keys")
def create_api_key(payload: CreateApiKeyPayload, db: TraceStore = Depends(store)) -> dict[str, Any]:
    # Local-first bootstrap endpoint. Protect externally with network/auth at deployment time.
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


@app.get("/usage")
def usage_summary(
    project_id_filter: str | None = Query(default=None, alias="project_id"),
    project_id: str = Depends(resolve_project),
    db: TraceStore = Depends(store),
) -> dict[str, Any]:
    selected_project = project_id_filter or project_id
    return db.usage_summary(project_id=selected_project)
