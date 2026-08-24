"""Deterministic paired AgentLoop experiment with no network or model API."""

from __future__ import annotations

import json
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace

RUNS = Path("runs/research_example")
RUN_ID = "run_research_example"
START = "2026-01-01T00:00:00+00:00"
END = "2026-01-01T00:00:01+00:00"


def _trace(condition: str, elapsed_ms: float, events: list[AgentEvent]) -> AgentTrace:
    trace = AgentTrace(
        name="parallel-retrieval-study",
        run_id=f"{RUN_ID}_{condition}",
        metadata={
            "synthetic": True,
            "source": "research_experiment_demo",
            "experiment_id": "parallel-retrieval-v1",
            "condition": condition,
            "dataset": "synthetic-two-source",
            "task_id": "task-001",
            "seed": 0,
            "prompt_version": "v1",
            "git_commit": "example",
        },
        started_at=START,
        ended_at=END,
        elapsed_ms=elapsed_ms,
    )
    for event in events:
        trace.add_event(event)
    return trace


def _tool(event_id: str, run_id: str, name: str, start: str, end: str, duration: float) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        run_id=run_id,
        event_type="tool_call",
        name=name,
        started_at=start,
        ended_at=end,
        duration_ms=duration,
    )


def main() -> None:
    baseline_run = f"{RUN_ID}_sequential"
    candidate_run = f"{RUN_ID}_parallel"
    baseline = _trace(
        "sequential",
        900.0,
        [
            _tool(
                "evt_baseline_a",
                baseline_run,
                "retrieve_a",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00.500000+00:00",
                500.0,
            ),
            _tool(
                "evt_baseline_b",
                baseline_run,
                "retrieve_b",
                "2026-01-01T00:00:00.500000+00:00",
                "2026-01-01T00:00:00.900000+00:00",
                400.0,
            ),
        ],
    )
    candidate = _trace(
        "parallel",
        500.0,
        [
            _tool(
                "evt_candidate_a",
                candidate_run,
                "retrieve_a",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00.500000+00:00",
                500.0,
            ),
            _tool(
                "evt_candidate_b",
                candidate_run,
                "retrieve_b",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00.400000+00:00",
                400.0,
            ),
        ],
    )

    RUNS.mkdir(parents=True, exist_ok=True)
    baseline_path = baseline.export_json(RUNS / "baseline.json")
    candidate_path = candidate.export_json(RUNS / "candidate.json")
    comparison = build_replay_report(baseline, candidate)
    comparison_path = RUNS / "comparison.json"
    comparison_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")

    print(f"baseline:  {baseline_path}")
    print(f"candidate: {candidate_path}")
    print(f"comparison: {comparison_path}")
    print(comparison["summary"])


if __name__ == "__main__":
    main()
