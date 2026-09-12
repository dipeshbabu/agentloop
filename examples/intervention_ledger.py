"""Generate a synthetic, offline intervention artifact without model API calls."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.intervention_service import create_stored_intervention
from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace


def synthetic_trace(name: str, duration_ms: int) -> AgentTrace:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = AgentTrace(
        name=name,
        run_id=f"run_{name}",
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=3 * duration_ms)).isoformat(),
        elapsed_ms=3 * duration_ms,
        metadata={"synthetic": True, "task_id": "example-task", "seed": 7},
    )
    for index in range(3):
        trace.add_event(
            AgentEvent(
                event_id=f"{name}-tool-{index}",
                run_id=trace.run_id,
                name="lookup",
                event_type="tool_call",
                duration_ms=duration_ms,
                started_at=(start + timedelta(milliseconds=index * duration_ms)).isoformat(),
                ended_at=(start + timedelta(milliseconds=(index + 1) * duration_ms)).isoformat(),
            )
        )
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/interventions-demo"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    baseline, candidate = synthetic_trace("baseline", 200), synthetic_trace("candidate", 100)
    db = SQLiteTraceStore(str(args.out / "ledger.db"))
    db.save_trace(baseline)
    db.save_trace(candidate)
    diagnosis = build_diagnosis(baseline)
    request = {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "target_finding_ids": [diagnosis["findings"][0]["finding_id"]],
        "intervention_type": "faster_lookup",
        "configuration": {"lookup_implementation": "synthetic-example"},
        "metadata": {"synthetic": True, "task_id": "example-task", "seed": 7},
        "gates": {"min_latency_improvement_pct": 10, "min_quality_score": 1},
        "quality_fixtures": [{"expected": "ok", "baseline_output": "ok", "candidate_output": "ok"}],
    }
    record = create_stored_intervention(db, request)
    assert record == create_stored_intervention(db, request)
    assert record["gates_passed"] is True
    for filename, payload in (
        ("baseline.json", baseline.to_dict()),
        ("candidate.json", candidate.to_dict()),
        ("diagnosis.json", diagnosis),
        ("request.json", request),
        ("intervention.json", record),
    ):
        (args.out / filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Synthetic evidence written to {args.out / 'intervention.json'}")
    print("These fixture timings demonstrate the ledger; they are not a performance benchmark.")


if __name__ == "__main__":
    main()
