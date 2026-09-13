"""Exercise the complete evidence workflow with deterministic synthetic traces."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.intervention_service import create_stored_intervention
from agentloop.optimizer import build_optimization_plan
from agentloop.store import SQLiteTraceStore
from agentloop.studies import study_to_markdown, summarize_study
from agentloop.tracer import AgentTrace


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _trace(condition: str, task: int, seed: int) -> AgentTrace:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    parallel = condition == "candidate"
    failed = parallel and task == 2 and seed == 1
    duration = 200 + task * 20 + seed * 10
    model_start = duration if parallel else duration * 3
    run_id = f"{condition}-task{task}-seed{seed}"
    trace = AgentTrace(
        name=condition,
        run_id=run_id,
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=model_start + 100)).isoformat(),
        elapsed_ms=model_start + 100,
        metadata={
            "synthetic": True,
            "task_id": task,
            "seed": seed,
            "output": "bad" if failed else "ok",
            "success": not failed,
            "quality_score": 0.0 if failed else 1.0,
        },
    )
    for index in range(3):
        begin = 0 if parallel else index * duration
        trace.add_event(
            AgentEvent(
                event_id=f"{run_id}-tool{index}",
                run_id=run_id,
                event_type="tool_call",
                name="lookup",
                started_at=(start + timedelta(milliseconds=begin)).isoformat(),
                ended_at=(start + timedelta(milliseconds=begin + duration)).isoformat(),
                duration_ms=duration,
                status="error" if failed and index == 0 else "ok",
                metadata={
                    "parallel_safe": True,
                    **({"error_type": "tool_error"} if failed and index == 0 else {}),
                },
            )
        )
    trace.add_event(
        AgentEvent(
            event_id=f"{run_id}-model",
            run_id=run_id,
            event_type="model_call",
            name="answer",
            started_at=(start + timedelta(milliseconds=model_start)).isoformat(),
            ended_at=trace.ended_at,
            duration_ms=100,
            model="unpriced-example" if task == 2 and seed == 1 else "gpt-4o-mini",
            input_tokens=100,
            output_tokens=20,
            token_provenance="user_supplied",
        )
    )
    return trace


def run(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    db = SQLiteTraceStore(str(out / "ledger.db"))
    records = []
    for task in range(3):
        for seed in range(2):
            baseline, candidate = _trace("baseline", task, seed), _trace("candidate", task, seed)
            for trace in (baseline, candidate):
                db.save_trace(trace)
                trace.export_json(out / trace.name / f"{trace.run_id}.json")
            diagnosis = build_diagnosis(baseline)
            target = next(
                finding
                for finding in diagnosis["findings"]
                if finding["type"] == "parallelize_tools"
            )
            request = {
                "baseline_run_id": baseline.run_id,
                "candidate_run_id": candidate.run_id,
                "target_finding_ids": [target["finding_id"]],
                "intervention_type": "parallelize_tools",
                "configuration": {"max_concurrency": 3},
                "metadata": {"synthetic": True, "task_id": task, "seed": seed},
                "gates": {"min_latency_improvement_pct": 5, "min_quality_score": 1},
                "quality_fixtures": [{"id": "answer", "expected": "ok"}],
            }
            record = create_stored_intervention(db, request)
            assert create_stored_intervention(db, request) == record
            records.append(record)
            _write(out / "interventions" / f"{record['intervention_id']}.json", record)
            _write(out / "diagnoses" / f"{baseline.run_id}.json", diagnosis)
            _write(out / "requests" / f"{baseline.run_id}.json", request)
            if task == seed == 0:
                payload = {
                    "trace": baseline.to_dict(),
                    "report": baseline.report(),
                    "diagnosis": diagnosis,
                    "optimization": build_optimization_plan(baseline),
                    "replay": record["measured"],
                }
                (out / "example.html").write_text(analysis_to_html(payload), encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "name": "Synthetic parallelization interventions",
        "baseline": "baseline",
        "conditions": {"baseline": ["baseline/*.json"], "candidate": ["candidate/*.json"]},
        "pairing_keys": ["task_id", "seed"],
        "bootstrap": {"samples": 200, "seed": 7, "confidence": 0.95},
    }
    _write(out / "study.json", manifest)
    study = summarize_study(out / "study.json")
    _write(out / "study-results.json", study)
    (out / "study.md").write_text(study_to_markdown(study), encoding="utf-8")
    passed = sum(record["gates_passed"] for record in records)
    summary = {
        "synthetic": True,
        "intervention_type": "parallelize_tools",
        "intervention_count": len(records),
        "configured_gates_passed_count": passed,
        "configured_gate_pass_rate": passed / len(records),
        "criterion": "the configured performance and quality gates passed",
        "interpretation": "Fixture results demonstrate artifact linkage, not empirical recommendation precision or calibration.",
        "intervention_ids": [record["intervention_id"] for record in records],
    }
    assert study["comparisons"]["candidate"]["pair_count"] == 6
    assert passed == 5
    assert study["comparisons"]["candidate"]["metrics"]["cost_usd"]["missing_count"] == 1
    _write(out / "outcomes.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/evidence-workflow"))
    result = run(parser.parse_args().out)
    print(
        f"Synthetic workflow: {result['intervention_count']} linked interventions, {result['configured_gates_passed_count']} configured gate passes."
    )
    print(result["interpretation"])


if __name__ == "__main__":
    main()
