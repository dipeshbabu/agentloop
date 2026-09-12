from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.events import AgentEvent
from agentloop.studies import (
    StudyValidationError,
    study_to_markdown,
    summarize_study,
    summarize_values,
)
from agentloop.tracer import AgentTrace


def write_trace(
    directory, run_id, task, seed, duration, *, failed=False, model="gpt-4o", quality=1.0
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(milliseconds=duration)
    trace = AgentTrace(
        name=run_id,
        run_id=run_id,
        started_at=start.isoformat(),
        ended_at=end.isoformat(),
        elapsed_ms=duration,
        metadata={"task_id": task, "seed": seed, "quality_score": quality, "synthetic": True},
    )
    trace.add_event(
        AgentEvent(
            event_id=f"span-{run_id}",
            run_id=run_id,
            name="model",
            event_type="model_call",
            started_at=start.isoformat(),
            ended_at=end.isoformat(),
            duration_ms=duration,
            model=model,
            input_tokens=100,
            output_tokens=10,
            token_provenance="user_supplied",
            status="error" if failed else "ok",
            metadata={"error_type": "timeout"} if failed else {},
        )
    )
    path = directory / f"{run_id}.json"
    trace.export_json(path)
    return path


def manifest(tmp_path, **changes):
    payload = {
        "schema_version": "1.0",
        "name": "paired study",
        "baseline": "baseline",
        "conditions": {"baseline": ["baseline/*.json"], "candidate": ["candidate/*.json"]},
        "pairing_keys": ["task_id", "seed"],
        **changes,
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def study(tmp_path):
    for task in ("a", "b"):
        for seed in (1, 2):
            write_trace(tmp_path / "baseline", f"before-{task}-{seed}", task, seed, 100)
            write_trace(tmp_path / "candidate", f"after-{task}-{seed}", task, seed, 80)
    return manifest(tmp_path)


def test_multiple_tasks_and_repetitions_produce_reproducible_paired_summaries(study):
    report = summarize_study(study)
    condition = report["conditions"]["baseline"]
    assert condition["run_count"] == 4
    assert condition["operation_counts"] == {"model": 4}
    assert condition["metrics"]["runtime_ms"]["mean"] == 100
    comparison = report["comparisons"]["candidate"]
    assert comparison["pair_count"] == 4
    assert comparison["unmatched"] == []
    latency = comparison["metrics"]["runtime_ms"]
    assert latency["mean"] == latency["median"] == latency["p05"] == latency["p95"] == -20
    assert summarize_study(study) == report
    assert "synthetic data" in study_to_markdown(report)


def test_quantiles_use_documented_interpolation_and_missing_denominators():
    summary = summarize_values([4, 1, None, 3, 2])
    assert summary["count"] == 4 and summary["missing_count"] == 1
    assert summary["mean"] == summary["median"] == 2.5
    assert summary["p05"] == pytest.approx(1.15)
    assert summary["p95"] == pytest.approx(3.85)
    assert summarize_values([None])["mean"] is None
    assert summarize_values([7])["p95"] == 7


def test_bootstrap_with_variable_deltas_has_nonzero_interval(study):
    for task, seed, duration in [("a", 1, 20), ("a", 2, 60), ("b", 1, 120), ("b", 2, 200)]:
        write_trace(study.parent / "candidate", f"after-{task}-{seed}", task, seed, duration)
    path = manifest(study.parent, bootstrap={"samples": 300, "seed": 5})
    report = summarize_study(path)
    metric = report["comparisons"]["candidate"]["metrics"]["runtime_ms"]
    interval = metric["bootstrap_interval"]
    assert -80 <= interval["lower"] < metric["mean"] < interval["upper"] <= 100
    assert report == summarize_study(path)


def test_absent_quality_is_missing_and_failed_execution_overrides_success(study):
    path = study.parent / "candidate" / "after-a-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metadata"].pop("quality_score")
    payload["metadata"]["success"] = True
    payload["events"][0]["status"] = "error"
    path.write_text(json.dumps(payload), encoding="utf-8")
    metrics = summarize_study(study)["conditions"]["candidate"]["metrics"]
    assert metrics["quality_score"]["missing_count"] == 1
    assert metrics["success"]["mean"] == 0.75


def test_missing_pair_and_missing_metadata_are_reported(study):
    path = study.parent / "candidate" / "after-a-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metadata"].pop("task_id")
    path.write_text(json.dumps(payload), encoding="utf-8")
    comparison = summarize_study(study)["comparisons"]["candidate"]
    assert comparison["pair_count"] == 3
    assert {run["reason"] for run in comparison["unmatched"]} == {
        "missing_counterpart",
        "missing_or_invalid_pairing_metadata",
    }


def test_duplicate_pairing_key_excludes_all_ambiguous_runs(study):
    write_trace(study.parent / "candidate", "another-a-1", "a", 1, 60)
    report = summarize_study(study)
    comparison = report["comparisons"]["candidate"]
    assert comparison["pair_count"] == 3
    assert len(comparison["unmatched"]) == 3
    assert {run["reason"] for run in comparison["unmatched"]} == {"duplicate_pairing_key"}
    assert report["conditions"]["candidate"]["run_count"] == 5


def test_duplicate_run_ids_are_rejected_across_conditions(study):
    write_trace(study.parent / "candidate", "before-a-1", "a", 3, 80)
    with pytest.raises(StudyValidationError, match="duplicate run_id"):
        summarize_study(study)


def test_failed_runs_and_unknown_cost_keep_their_denominators(study):
    write_trace(
        study.parent / "candidate",
        "after-a-1",
        "a",
        1,
        200,
        failed=True,
        model="unknown-model",
        quality=0,
    )
    report = summarize_study(study)
    condition = report["conditions"]["candidate"]
    assert condition["metrics"]["success"]["mean"] == 0.75
    assert condition["metrics"]["quality_score"]["mean"] == 0.75
    assert condition["failure_categories"] == {"timeout": 1}
    assert condition["cost_status_counts"] == {"complete": 3, "unknown": 1}
    assert condition["metrics"]["cost_usd"]["count"] == 3
    cost = report["comparisons"]["candidate"]["metrics"]["cost_usd"]
    assert cost["count"] == 3 and cost["missing_count"] == 1
    assert any(
        pair["deltas"]["cost_usd"] is None for pair in report["comparisons"]["candidate"]["pairs"]
    )


def test_estimated_tokens_cannot_supply_evaluable_cost_pairs(study):
    path = study.parent / "candidate" / "after-a-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["token_provenance"] = "estimated_words"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = summarize_study(study)
    assert report["conditions"]["candidate"]["token_status_counts"]["estimated"] == 1
    assert report["comparisons"]["candidate"]["metrics"]["cost_usd"]["missing_count"] == 1


def test_seeded_bootstrap_is_optional_and_deterministic(study):
    path = manifest(study.parent, bootstrap={"samples": 200, "seed": 12, "confidence": 0.9})
    first = summarize_study(path)
    assert first == summarize_study(path)
    interval = first["comparisons"]["candidate"]["metrics"]["runtime_ms"]["bootstrap_interval"]
    assert interval["lower"] == interval["upper"] == -20
    assert interval["pair_count"] == 4 and interval["status"] == "computed"
    assert "exchangeable independent pairs" in first["interpretation"]


def test_zero_matches_are_reported_without_fabricated_statistics(study):
    manifest_path = manifest(study.parent, pairing_keys=["missing-key"], bootstrap={"samples": 5})
    comparison = summarize_study(manifest_path)["comparisons"]["candidate"]
    assert comparison["pair_count"] == 0
    assert len(comparison["unmatched"]) == 8
    assert comparison["metrics"]["runtime_ms"]["mean"] is None
    assert (
        comparison["metrics"]["runtime_ms"]["bootstrap_interval"]["status"] == "insufficient_pairs"
    )


def test_overlapping_globs_and_manifest_order_do_not_duplicate_data(study):
    original = summarize_study(study)
    updated = manifest(
        study.parent,
        conditions={
            "candidate": ["candidate/*.json"],
            "baseline": ["baseline/before-*.json", "baseline/*.json"],
        },
    )
    changed = summarize_study(updated)
    assert changed["conditions"] == original["conditions"]
    assert changed["comparisons"] == original["comparisons"]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2.0"},
        {"pairing_keys": []},
        {"baseline": "missing"},
        {"bootstrap": {"samples": True}},
        {"bootstrap": {"confidence": 1}},
        {"conditions": {"baseline": ["missing.json"], "candidate": ["candidate/*.json"]}},
    ],
)
def test_invalid_manifest_is_actionable(study, changes):
    with pytest.raises(StudyValidationError):
        summarize_study(manifest(study.parent, **changes))


def test_cli_exports_json_and_escaped_markdown(study, tmp_path):
    path = manifest(study.parent, name="study <script> | unsafe")
    markdown, machine = tmp_path / "report.md", tmp_path / "report.json"
    result = CliRunner().invoke(
        app, ["study", "summarize", str(path), "--out", str(markdown), "--json-out", str(machine)]
    )
    assert result.exit_code == 0, result.output
    assert (
        json.loads(machine.read_text(encoding="utf-8"))["comparisons"]["candidate"]["pair_count"]
        == 4
    )
    rendered = markdown.read_text(encoding="utf-8")
    assert "<script>" not in rendered
    assert "Matched pairs: 4" in rendered
