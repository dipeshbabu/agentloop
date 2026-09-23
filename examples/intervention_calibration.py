"""Build synthetic calibration artifacts; all fixtures are excluded from empirical fitting."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from agentloop.calibration import calibration_to_markdown, summarize_calibration
from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.tracer import AgentTrace


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def fixture_trace(task, side, duration, *, failed=False, unscored=False):
    start = datetime(2026, 1, 1 if side == "baseline" else 3, tzinfo=timezone.utc)
    parallel = side == "candidate"
    trace = AgentTrace(
        name=side,
        run_id=f"{task}-{side}",
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=duration)).isoformat(),
        elapsed_ms=duration,
        metadata={
            "synthetic": True,
            "task_id": task,
            "repetition": 0,
            "success": not failed,
            "quality_score": None if unscored else 0 if failed else 1,
        },
    )
    for index in range(3):
        begin = start + timedelta(milliseconds=0 if parallel else index * 200)
        end = begin + timedelta(milliseconds=200)
        trace.add_event(
            AgentEvent(
                event_id=f"{trace.run_id}-tool{index}",
                run_id=trace.run_id,
                event_type="tool_call",
                name="lookup",
                started_at=begin.isoformat(),
                ended_at=end.isoformat(),
                duration_ms=200,
                metadata={"parallel_safe": True},
            )
        )
    model_start = 200 if parallel else 600
    trace.add_event(
        AgentEvent(
            event_id=f"{trace.run_id}-model",
            run_id=trace.run_id,
            event_type="model_call",
            name="answer",
            started_at=(start + timedelta(milliseconds=model_start)).isoformat(),
            ended_at=trace.ended_at,
            duration_ms=duration - model_start,
            model="gpt-4o-mini",
            input_tokens=100,
            output_tokens=20,
            token_provenance="user_supplied",
            status="error" if failed else "ok",
        )
    )
    return trace


def run(out: Path):
    registrations = []
    for index, name in enumerate(("fit-a", "fit-b", "held-a", "held-b", "not-selected", "missing")):
        baseline = fixture_trace(name, "baseline", 1000)
        baseline.export_json(out / f"{name}-baseline.json")
        diagnosis = build_diagnosis(baseline)
        finding = next(
            item for item in diagnosis["findings"] if item["type"] == "parallelize_tools"
        )
        # Persist the original prediction before constructing the candidate artifact.
        write(out / f"{name}-prediction.json", finding)
        outcome = None
        if index < 4:
            candidate = fixture_trace(
                name,
                "candidate",
                (600, 1200, 700, 800)[index],
                failed=index == 1,
                unscored=index == 3,
            )
            candidate.export_json(out / f"{name}-candidate.json")
            record = build_intervention(
                baseline,
                candidate,
                target_finding_ids=[finding["finding_id"]],
                intervention_type="parallelize_tools",
                configuration={"max_concurrency": 3},
                metadata={"synthetic": True},
                diagnosis=diagnosis,
            ).to_dict()
            write(out / f"{name}-record.json", record)
            outcome = {
                "record": f"{name}-record.json",
                "baseline_trace": f"{name}-baseline.json",
                "candidate_trace": f"{name}-candidate.json",
                "recorded_at": "2026-01-04T00:00:00Z",
                "status": "failed" if index == 1 else "completed",
            }
        registrations.append(
            {
                "case_id": name,
                "task_id": name,
                "task_sha256": sha256(name.encode()).hexdigest(),
                "repetition": 0,
                "split": "fit" if index < 2 else "held_out",
                "selection": "unselected" if index == 4 else "selected",
                "selection_reason": "fixture_not_selected" if index == 4 else "fixture_planned",
                "synthetic": True,
                "context": {
                    "workload": "synthetic-calibration-fixture",
                    "model": "fixture-model-config",
                    "provider": "offline-fixture",
                    "environment": "fixture-environment-v1",
                    "scorer": "fixture-scores-v1",
                    "policy": None,
                    "cost_basis": "calculated",
                    "pricing": "fixture-pricing-snapshot",
                    "quality_gate": {"min_score": 0.9, "max_regression": 0},
                    "intervention": {
                        "type": "parallelize_tools",
                        "configuration": {"max_concurrency": 3},
                    },
                },
                "prediction": finding,
                "prediction_recorded_at": "2026-01-02T00:00:00Z",
                "outcome": outcome,
            }
        )
    manifest = {
        "schema_version": "1.0",
        "name": "Synthetic historical calibration contract",
        "as_of": "2026-01-05T00:00:00Z",
        "valid_until": "2026-02-01T00:00:00Z",
        "fit_method": "scale",
        "min_fit_tasks": 2,
        "selection_inventory_complete": True,
        "bootstrap": {"samples": 200, "seed": 7, "confidence": 0.95},
        "registrations": registrations,
    }
    write(out / "manifest.json", manifest)
    report = summarize_calibration(out / "manifest.json")
    write(out / "report.json", report)
    (out / "report.md").write_text(calibration_to_markdown(report), encoding="utf-8")
    assert report["synthetic_excluded_count"] == 6
    assert all(
        metric["fit"]["status"] == "insufficient_tasks"
        for cohort in report["cohorts"]
        for metric in cohort["metrics"].values()
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/intervention-calibration"))
    report = run(parser.parse_args().out)
    print(
        f"Synthetic fixture: {report['registration_count']} registrations retained; {report['synthetic_excluded_count']} excluded from empirical calibration. No runtime coefficients changed."
    )
