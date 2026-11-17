from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from agentloop.events import AgentEvent
from agentloop.improvement import capture, start
from agentloop.repetitions import FINDING_TYPE, add_repetition_findings, repetition_findings
from agentloop.tracer import AgentTrace

ORIGIN = datetime(2026, 11, 17, tzinfo=timezone.utc)


def attempt(steps, *, tokens_per_step=1000, step_ms=100.0):
    """Build a trace from a step plan such as ["model", "tool:read a", "model"]."""
    trace = AgentTrace("repetition fixture")
    clock = 0.0
    for index, step in enumerate(steps):
        kind, _, text = step.partition(":")
        model = kind == "model"
        trace.add_event(
            AgentEvent(
                event_id=f"evt_{index}",
                run_id=trace.run_id,
                event_type="model_call" if model else "tool_call",
                name="model" if model else "shell",
                started_at=(ORIGIN + timedelta(milliseconds=clock)).isoformat(),
                ended_at=(ORIGIN + timedelta(milliseconds=clock + step_ms)).isoformat(),
                duration_ms=step_ms,
                input_tokens=tokens_per_step if model else 0,
                token_provenance="provider" if model and tokens_per_step else None,
                input_text=text or None,
            )
        )
        clock += step_ms
    trace.finish()
    trace.ended_at = (ORIGIN + timedelta(milliseconds=clock)).isoformat()
    trace.elapsed_ms = clock
    return trace


TYPICAL = ["model", "tool:read pyproject.toml", "model"]
DETOUR = ["model", "tool:read notes.md", "model", "tool:read pyproject.toml", "model"]


def test_extra_model_step_with_more_tokens_is_reported_on_the_outlier():
    traces = {
        ("lookup", 0): attempt(TYPICAL),
        ("lookup", 1): attempt(DETOUR),
        ("lookup", 2): attempt(TYPICAL),
    }
    found = repetition_findings(traces)
    assert list(found) == [("lookup", 1)]
    finding = found[("lookup", 1)][0]
    assert finding["type"] == FINDING_TYPE
    assert finding["title"] == (
        "Repetition 1 of lookup took 1 more model step and 1 more tool call "
        "than its typical repetition"
    )
    assert finding["savings"]["estimated_input_tokens"] == 1000
    assert finding["confidence"] == "low"
    excerpts = {row["input_excerpt"] for row in finding["evidence"]}
    assert "read notes.md" in excerpts
    assert "read pyproject.toml" not in excerpts


def test_slower_run_without_extra_tokens_is_not_reported():
    traces = {
        ("lookup", 0): attempt(TYPICAL),
        ("lookup", 1): attempt(
            ["model", "model", "tool:read pyproject.toml", "model"], step_ms=400
        ),
    }
    traces[("lookup", 1)].events[1].input_tokens = 0
    assert repetition_findings(traces) == {}


def test_runtime_decides_when_token_counts_are_unavailable():
    traces = {
        ("lookup", 0): attempt(TYPICAL, tokens_per_step=0),
        ("lookup", 1): attempt(DETOUR, tokens_per_step=0),
    }
    found = repetition_findings(traces)
    assert list(found) == [("lookup", 1)]
    assert found[("lookup", 1)][0]["savings"]["estimated_latency_savings_ms"] == 200


def test_same_steps_or_single_repetitions_are_not_compared():
    assert repetition_findings({("a", 0): attempt(DETOUR)}) == {}
    assert repetition_findings({("a", 0): attempt(TYPICAL), ("a", 1): attempt(TYPICAL)}) == {}


def test_recurring_detours_raise_confidence():
    traces = {
        ("first", 0): attempt(TYPICAL),
        ("first", 1): attempt(DETOUR),
        ("second", 0): attempt(DETOUR),
        ("second", 1): attempt(TYPICAL),
    }
    found = repetition_findings(traces)
    assert {key: value[0]["confidence"] for key, value in found.items()} == {
        ("first", 1): "medium",
        ("second", 0): "medium",
    }


def test_findings_merge_into_ranked_diagnoses():
    from agentloop.findings import build_diagnosis

    traces = {("lookup", 0): attempt(TYPICAL), ("lookup", 1): attempt(DETOUR)}
    diagnoses = add_repetition_findings(
        {key: build_diagnosis(trace) for key, trace in traces.items()}, traces
    )
    merged = [item for item in diagnoses[("lookup", 1)]["findings"] if item["type"] == FINDING_TYPE]
    assert len(merged) == 1
    assert merged[0]["ranking"]["priority_rank"] >= 1
    assert diagnoses[("lookup", 1)]["summary"]["finding_count"] >= 1
    assert not any(item["type"] == FINDING_TYPE for item in diagnoses[("lookup", 0)]["findings"])


WORKER = """import json,sys
from datetime import datetime,timedelta,timezone
from agentloop.events import AgentEvent
from agentloop.tracer import AgentTrace
request=json.loads(sys.stdin.readline())
detour=request["task_id"]=="first" and request["repetition"]==1
steps=["model","tool:read source","model"]
if detour:
    steps=["model","tool:read notes","model","tool:read source","model"]
trace=AgentTrace("worker",metadata={"synthetic":True,"output":request["input"]})
origin=datetime(2026,11,17,tzinfo=timezone.utc)
for index,step in enumerate(steps):
    kind,_,text=step.partition(":")
    trace.add_event(AgentEvent(event_id=f"evt_{index}",run_id=trace.run_id,event_type="model_call" if kind=="model" else "tool_call",name=kind,started_at=(origin+timedelta(seconds=index)).isoformat(),ended_at=(origin+timedelta(seconds=index+1)).isoformat(),duration_ms=1000,input_tokens=1000 if kind=="model" else 0,token_provenance="provider" if kind=="model" else None,input_text=text or None))
trace.finish()
trace.export_json(request["trace_path"])
"""


def test_start_offers_the_detour_as_a_focus(tmp_path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "Repetition tasks",
                "repetitions": 2,
                "tasks": [
                    {"id": "first", "input": "one", "expected": "one"},
                    {"id": "second", "input": "two", "expected": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    capture(tasks, tmp_path / "baseline", [sys.executable, str(worker)])
    specification = start(
        tasks, tmp_path / "baseline/capture.json", tmp_path / "session", focus=FINDING_TYPE
    )
    assert specification["focus"] == FINDING_TYPE
    flagged = [case for case in specification["cases"] if case["finding_ids"]]
    assert [(case["task_id"], case["repetition"]) for case in flagged] == [("first", 1)]
    plan = (tmp_path / "session/plan.md").read_text(encoding="utf-8")
    assert "took 1 more model step and 1 more tool call" in plan
    assert "read notes" in plan
