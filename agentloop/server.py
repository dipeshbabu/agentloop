from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agentloop.config import get_api_key, require_api_key
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace

RUNS_DIR = Path("runs")
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AgentLoop API", version="0.3.0")


class TracePayload(BaseModel):
    name: str
    run_id: str
    started_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


def verify_api_key(x_agentloop_key: str | None = Header(default=None)) -> None:
    if not require_api_key():
        return
    expected = get_api_key()
    if not expected:
        raise HTTPException(status_code=500, detail="AGENTLOOP_REQUIRE_API_KEY is enabled but AGENTLOOP_API_KEY is not set")
    if x_agentloop_key != expected:
        raise HTTPException(status_code=401, detail="invalid AgentLoop API key")


def _find_trace_path(run_id: str) -> Path:
    matches = list(RUNS_DIR.glob(f"{run_id}.json"))
    if not matches:
        matches = [p for p in RUNS_DIR.glob("*.json") if run_id in p.name]
    if not matches:
        raise HTTPException(status_code=404, detail="trace not found")
    return matches[0]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/traces", dependencies=[Depends(verify_api_key)])
def ingest_trace(payload: TracePayload) -> dict[str, Any]:
    trace = AgentTrace.from_dict(payload.model_dump())
    path = RUNS_DIR / f"{trace.run_id}.json"
    trace.export_json(path)
    return {"ok": True, "run_id": trace.run_id, "path": str(path), "report": trace.report()}


@app.get("/traces", dependencies=[Depends(verify_api_key)])
def list_traces() -> dict[str, Any]:
    traces = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            traces.append({"path": str(path), "run_id": data.get("run_id"), "name": data.get("name")})
        except Exception:
            continue
    return {"traces": traces}


@app.get("/traces/{run_id}/report", dependencies=[Depends(verify_api_key)])
def get_report(run_id: str) -> dict[str, Any]:
    return AgentTrace.from_json(_find_trace_path(run_id)).report()


@app.get("/traces/{run_id}/optimize", dependencies=[Depends(verify_api_key)])
def optimize_trace(run_id: str) -> dict[str, Any]:
    trace = AgentTrace.from_json(_find_trace_path(run_id))
    return build_optimization_plan(trace)
