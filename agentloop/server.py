from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agentloop.tracer import AgentTrace

RUNS_DIR = Path("runs")
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AgentLoop API", version="0.2.0")


class TracePayload(BaseModel):
    name: str
    run_id: str
    started_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/traces")
def ingest_trace(payload: TracePayload) -> dict[str, Any]:
    trace = AgentTrace.from_dict(payload.model_dump())
    path = RUNS_DIR / f"{trace.run_id}.json"
    trace.export_json(path)
    return {"ok": True, "run_id": trace.run_id, "path": str(path), "report": trace.report()}


@app.get("/traces")
def list_traces() -> dict[str, Any]:
    traces = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            traces.append({"path": str(path), "run_id": data.get("run_id"), "name": data.get("name")})
        except Exception:
            continue
    return {"traces": traces}


@app.get("/traces/{run_id}/report")
def get_report(run_id: str) -> dict[str, Any]:
    matches = list(RUNS_DIR.glob(f"{run_id}.json"))
    if not matches:
        matches = [p for p in RUNS_DIR.glob("*.json") if run_id in p.name]
    if not matches:
        raise HTTPException(status_code=404, detail="trace not found")
    return AgentTrace.from_json(matches[0]).report()
