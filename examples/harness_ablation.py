"""Construct synthetic report fixtures; this script does not measure agent performance."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from agentloop.ablation_protocol import VERSION_KEYS, AblationProtocol
from agentloop.ablations import ablation_to_markdown, summarize_ablation
from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.tracer import AgentTrace


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def fixture_trace(row):
    start = datetime.fromisoformat(row["started_at"])
    duration = row["metrics"]["latency_ms"]
    trace = AgentTrace(
        name=row["condition"],
        run_id=row["observation_id"],
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=duration)).isoformat(),
        elapsed_ms=duration,
        metadata={
            "synthetic": True,
            "fixture_source": "harness_ablation",
            "task_id": row["task_id"],
            "repetition": row["repetition"],
            "cache_condition": row["cache_condition"],
            "success": row["success"],
            "quality_score": row["quality_score"],
        },
    )
    for index in range(4):
        is_model = index == 3
        unknown = row["status"] != "completed" and is_model
        begin = start + timedelta(milliseconds=duration * index / 4)
        end = start + timedelta(milliseconds=duration * (index + 1) / 4)
        trace.add_event(
            AgentEvent(
                event_id=f"{trace.run_id}-{index}",
                run_id=trace.run_id,
                event_type="model_call" if is_model else "tool_call",
                name="answer" if is_model else "lookup",
                started_at=begin.isoformat(),
                ended_at=end.isoformat(),
                duration_ms=duration / 4,
                model=("fixture-unpriced" if unknown else "gpt-4o-mini") if is_model else None,
                input_tokens=0 if unknown or not is_model else 100,
                output_tokens=0 if unknown or not is_model else 20,
                token_provenance="unavailable"
                if unknown
                else "user_supplied"
                if is_model
                else None,
                status="error" if unknown else "ok",
                metadata={"parallel_safe": not is_model},
            )
        )
    return trace


def run(out: Path):
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
        "shadow": {"mode": "shadow", "policies": {"fixture_guard": "fixture-v1"}},
        "enforce": {"mode": "enforce", "policies": {"fixture_guard": "fixture-v1"}},
    }
    tasks = {
        name: {
            "split": "pilot" if name == "pilot" else "held_out",
            "input_sha256": sha256(name.encode()).hexdigest(),
        }
        for name in ("pilot", "a", "b")
    }
    schedule = []
    for task in tasks:
        for repetition in ("0", "1"):
            order = list(conditions)
            shift = len(schedule) % len(order)
            schedule.append(
                {
                    "task_id": task,
                    "repetition": repetition,
                    "cache_condition": "cold",
                    "order": order[shift:] + order[:shift],
                }
            )
    spec = {
        "schema_version": "1.0",
        "name": "Synthetic four-condition harness ablation",
        "workload_id": "offline-report-fixture",
        "permission_ref": "synthetic fixture, no user data",
        "frozen_at": "2026-01-01T00:00:00+00:00",
        "synthetic": True,
        "versions": {key: "fixture-v1" for key in VERSION_KEYS},
        "tasks": tasks,
        "conditions": conditions,
        "schedule": schedule,
        "quality_gate": {"min_score": 0.9, "max_regression": 0},
        "bootstrap": {"samples": 200, "seed": 7, "confidence": 0.95},
    }
    protocol = AblationProtocol.freeze(spec)
    write(out / "protocol.json", protocol.to_dict())
    rows, traces, diagnosis_snapshots = [], {}, {}
    for slot_index, slot in enumerate(schedule):
        for position, condition in enumerate(slot["order"]):
            if slot["task_id"] == "b" and slot["repetition"] == "0" and condition == "enforce":
                continue  # Retain a planned but missing result in every relevant denominator.
            stopped = (
                slot["task_id"] == "a" and slot["repetition"] == "1" and condition == "enforce"
            )
            unscored = (
                slot["task_id"] == "b" and slot["repetition"] == "1" and condition == "enforce"
            )
            identity = f"{slot['task_id']}-{slot['repetition']}-{condition}"
            row = {
                "observation_id": identity,
                "protocol_hash": protocol.protocol_hash,
                "task_id": slot["task_id"],
                "repetition": slot["repetition"],
                "cache_condition": slot["cache_condition"],
                "condition": condition,
                "position": position,
                "started_at": (
                    datetime(2026, 1, 2, tzinfo=timezone.utc)
                    + timedelta(minutes=slot_index * 4 + position)
                ).isoformat(),
                "versions": dict(spec["versions"]),
                "reset_confirmed": True,
                "provider_seed": int(slot["repetition"]),
                "status": "stopped" if stopped else "completed",
                "stop_reason": "fixture_stop" if stopped else None,
                "success": not stopped,
                "quality_score": None if unscored else 0 if stopped else 1,
                "metrics": {
                    "latency_ms": 5
                    if stopped
                    else {"off": 1000, "trace": 1100, "shadow": 1200, "enforce": 900}[condition],
                    "tokens": None if stopped else 120,
                    "cost_usd": None if stopped else 0.000027,
                    "model_calls": 1,
                    "tool_calls": 3,
                    "retries": 0,
                    "policy_eval_ms": 1 if condition in {"shadow", "enforce"} else 0,
                },
                "token_status": "unavailable" if stopped else "exact",
                "cost_status": "unknown" if stopped else "complete",
                "cost_basis": "unknown" if stopped else "calculated",
                "trace_run_id": None if condition == "off" else identity,
            }
            rows.append(row)
            if condition != "off":
                trace = fixture_trace(row)
                traces[identity] = trace
                trace.export_json(out / condition / f"{identity}.json")
                if condition == "trace":
                    diagnosis_snapshots[identity] = build_diagnosis(trace)
                    write(out / "diagnoses" / f"{identity}.json", diagnosis_snapshots[identity])
    manifest = {
        "schema_version": "1.0",
        "name": "Synthetic traced arms",
        "baseline": "trace",
        "conditions": {name: [f"{name}/*.json"] for name in ("trace", "shadow", "enforce")},
        "pairing_keys": ["task_id", "repetition", "cache_condition"],
    }
    write(out / "study.json", manifest)
    # A synthetic ledger link exercises the existing immutable artifact format.
    # It does not assert that the fixture guard implements a recommended rewrite.
    baseline, candidate = traces["pilot-0-trace"], traces["pilot-0-enforce"]
    diagnosis = diagnosis_snapshots[baseline.run_id]
    target = next(item for item in diagnosis["findings"] if item["type"] == "parallelize_tools")
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[target["finding_id"]],
        intervention_type="synthetic_ablation_link",
        configuration={"fixture_only": True},
        metadata={"synthetic": True, "protocol_hash": protocol.protocol_hash},
        diagnosis=diagnosis,
    ).to_dict()
    write(out / "intervention.json", record)
    bundle = {
        "schema_version": "1.0",
        "protocol": protocol.to_dict(),
        "observations": rows,
        "study_manifest": "study.json",
        "interventions": ["intervention.json"],
    }
    write(out / "bundle.json", bundle)
    report = summarize_ablation(out / "bundle.json")
    write(out / "report.json", report)
    (out / "report.md").write_text(ablation_to_markdown(report), encoding="utf-8")
    held_out = next(
        item
        for item in report["comparisons"]
        if item["kind"] == "enforcement_effect" and item["split"] == "held_out"
    )
    assert held_out["quality_gate_counts"] == {
        "accepted": 1,
        "rejected": 1,
        "indeterminate": 1,
        "unmatched": 1,
    }
    assert report["planned_observation_count"] == 24 and report["observed_count"] == 23
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harness-ablation"))
    report = run(parser.parse_args().out)
    print(
        f"Synthetic fixture: {report['observed_count']} of {report['planned_observation_count']} planned observations; no real-agent performance claim."
    )
