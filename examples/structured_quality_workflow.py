"""Synthetic structured-output evidence through existing replay, ledger and studies."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop import (
    StageInfo,
    WorkflowInfo,
    attach_quality_report,
    record_operation,
    workflow_metadata,
)
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report, quality_report_to_markdown
from agentloop.replay import ReplayGates, replay_report_to_markdown
from agentloop.studies import study_to_markdown, summarize_study
from agentloop.tracer import AgentTrace


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def trace_fixture(task, side, *, failed=False):
    parallel = side == "candidate"
    start = datetime(2026, 1, 3 if parallel else 1, tzinfo=timezone.utc)
    elapsed = 110 if parallel else 310
    trace = AgentTrace(
        name=side,
        run_id=f"{task}-{side}",
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=elapsed)).isoformat(),
        elapsed_ms=elapsed,
        metadata=workflow_metadata(
            WorkflowInfo("structured-decision-fixture", "fixture-v1"),
            task_id=task,
            example_id=task,
            status="failed" if failed else "completed",
            metadata={"synthetic": True, "seed": 0},
        ),
    )
    references = []
    for index in range(3):
        begin = 0 if parallel else index * 100
        identity = f"{trace.run_id}-lookup-{index}"
        references.append(identity)
        record_operation(
            "lookup",
            kind="tool",
            duration_ms=100,
            started_at=(start + timedelta(milliseconds=begin)).isoformat(),
            ended_at=(start + timedelta(milliseconds=begin + 100)).isoformat(),
            stage=StageInfo(f"lookup-{index}", "fixture-v1"),
            depends_on=[],
            metadata={"parallel_safe": True},
            trace=trace,
            event_id=identity,
        )
    record_operation(
        "decision",
        kind="classifier",
        duration_ms=10,
        started_at=(start + timedelta(milliseconds=elapsed - 10)).isoformat(),
        ended_at=trace.ended_at,
        stage=StageInfo(task, "fixture-v1", output_schema_ref=f"schema:{task}:1"),
        depends_on=references,
        status="error" if failed else "ok",
        metadata={"error_type": "fixture_timeout"} if failed else {},
        trace=trace,
        event_id=f"{trace.run_id}-decision",
    )
    return trace


def run(out: Path):
    tasks = [
        (
            "routing",
            "billing",
            "support",
            "billing",
            {"type": "decision", "labels": ["billing", "support"]},
        ),
        (
            "extraction",
            {"name": "ExampleCo", "amount": 12},
            {"name": "ExampleCo"},
            {"name": "ExampleCo", "amount": 12},
            {"type": "fields"},
        ),
        ("matching", [["item-1", "entity-1"]], [], [["item-1", "entity-1"]], {"type": "matches"}),
        (
            "timeout",
            "support",
            "support",
            None,
            {"type": "decision", "labels": ["billing", "support"]},
        ),
        (
            "unmatched",
            "billing",
            "billing",
            None,
            {"type": "decision", "labels": ["billing", "support"]},
        ),
    ]
    records = []
    for task, expected, before_output, after_output, scorer in tasks:
        before = trace_fixture(task, "baseline")
        diagnosis = build_diagnosis(before)
        write(out / "predictions" / f"{task}.json", diagnosis)
        after = (
            None
            if task == "unmatched"
            else trace_fixture(task, "candidate", failed=task == "timeout")
        )
        fixture = {
            "schema_version": "2.0",
            "id": task,
            "expected": expected,
            "baseline_output": before_output,
            "scorer": scorer,
            "input_ref": f"fixture:{task}",
            "expected_ref": f"reference:{task}",
        }
        if after_output is not None:
            fixture["candidate_output"] = after_output
        if task == "timeout":
            fixture["candidate_status"] = "timed_out"
        quality = build_quality_report(
            [fixture], baseline_trace=before, candidate_trace=after, min_score=0.9
        )
        attach_quality_report(before, quality, side="baseline")
        before.export_json(out / "baseline" / f"{task}.json")
        write(out / "quality" / f"{task}.json", quality)
        (out / "quality" / f"{task}.md").write_text(
            quality_report_to_markdown(quality), encoding="utf-8"
        )
        if after is None:
            continue
        attach_quality_report(after, quality)
        after.export_json(out / "candidate" / f"{task}.json")
        target = next(item for item in diagnosis["findings"] if item["type"] == "parallelize_tools")
        record = build_intervention(
            before,
            after,
            target_finding_ids=[target["finding_id"]],
            intervention_type="parallelize_tools",
            configuration={"max_concurrency": 3},
            metadata={"synthetic": True},
            diagnosis=diagnosis,
            gates=ReplayGates(min_latency_improvement_pct=5, min_quality_score=0.9),
            quality_report=quality,
        ).to_dict()
        records.append(record)
        write(out / "interventions" / f"{task}.json", record)
        (out / "interventions" / f"{task}.md").write_text(
            replay_report_to_markdown(record["measured"]), encoding="utf-8"
        )
    write(
        out / "study.json",
        {
            "schema_version": "1.0",
            "name": "Synthetic structured workflow quality",
            "baseline": "baseline",
            "conditions": {"baseline": ["baseline/*.json"], "candidate": ["candidate/*.json"]},
            "pairing_keys": ["example_id", "seed"],
        },
    )
    report = summarize_study(out / "study.json")
    write(out / "study-results.json", report)
    (out / "study.md").write_text(study_to_markdown(report), encoding="utf-8")
    assert report["comparisons"]["candidate"]["pair_count"] == 4
    assert len(report["comparisons"]["candidate"]["unmatched"]) == 1
    assert report["conditions"]["candidate"]["metrics"]["quality_score"]["missing_count"] == 1
    assert sum(item["gates_passed"] for item in records) == 3
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/structured-quality"))
    report = run(parser.parse_args().out)
    print(
        "Synthetic fixture: routing, extraction and matching passed; timeout and unmatched cases retained. No application-benefit claim."
    )
