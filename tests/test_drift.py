from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from agentloop.aggregates import TraceAggregate
from agentloop.cli import app
from agentloop.drift import ReviewedBaseline, compare_drift
from agentloop.drift_types import DriftRule, QualityMetric, WindowSpec
from agentloop.drift_windows import WindowBuilder, validate_window
from agentloop.events import AgentEvent
from agentloop.retention import RetentionContext, RetentionPolicy, RetentionSession
from agentloop.tracer import AgentTrace
from agentloop.workflow_types import StageInfo, WorkflowInfo, operation_metadata, workflow_metadata

QUALITY = QualityMetric("accuracy", "quality", "v1", "fixture:exact-match")


def spec(day=1):
    return WindowSpec(
        f"window-{day}",
        "v1",
        f"2026-09-{day:02d}T00:00:00Z",
        f"2026-09-{day + 1:02d}T00:00:00Z",
        "workload:v1",
        "config:v1",
        "model:v1",
    )


def trace(
    index=0, *, day=1, latency=100, model="model-a", unknown=False, failed=False, outcome="accepted"
):
    ended = datetime(2026, 9, day, 12, tzinfo=timezone.utc)
    started = ended - timedelta(milliseconds=latency)
    metadata = workflow_metadata(
        WorkflowInfo("fixture", "v1"), status="failed" if failed else "completed"
    )
    metadata["agentloop.workflow"]["outcome"] = outcome
    result = AgentTrace(
        "fixture",
        run_id=f"run-{day}-{index}",
        metadata=metadata,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        elapsed_ms=latency,
    )
    details = operation_metadata("model", stage=StageInfo("answer", "v1", kind="model"))
    details["provider_reported_cost_usd"] = 0.25
    result.add_event(
        AgentEvent(
            "event",
            result.run_id,
            "model_call",
            "fixture",
            result.started_at,
            result.ended_at,
            latency,
            model=model,
            input_tokens=10,
            output_tokens=2,
            token_provenance=None if unknown else "provider",
            status="error" if failed else "ok",
            metadata=details,
        )
    )
    return result


def window(
    day=1,
    *,
    count=30,
    latency=100,
    model="model-a",
    score=1.0,
    unknown=False,
    include_findings=False,
    policy=None,
):
    builder = WindowBuilder(
        spec(day), quality_metrics=(QUALITY,), include_findings=include_findings
    )
    session = RetentionSession(policy) if policy else None
    for index in range(count):
        source = trace(index, day=day, latency=latency, model=model, unknown=unknown)
        if session:
            source = session.retain(
                source, context=RetentionContext(outcome="success", cohorts=("keep",))
            )
        builder.add(source, timeout=False, abstention=False, quality={"accuracy": score})
    return builder.to_dict()


def baseline(source=None, rules=None):
    return ReviewedBaseline(
        window() if source is None else source,
        rules=rules
        or (
            DriftRule("latency_mean_ms", absolute=10),
            DriftRule("quality:accuracy", direction="decrease", absolute=0.02),
        ),
        review_ref="fixture-review:v1",
    )


def metrics(report):
    return {metric["metric"]: metric for metric in report["metrics"]}


def test_regression_quality_loss_and_recovery_preserve_reference():
    reference = baseline()
    original = reference.to_dict()
    report = compare_drift(
        reference, window(2, latency=140, score=0.7), expected_baseline_sha256=reference.sha256
    )
    assert report["status"] == "alert"
    assert metrics(report)["latency_mean_ms"]["delta_interval"] == [40, 40]
    assert metrics(report)["quality:accuracy"]["status"] == "alert"
    assert report["baseline_window"]["window_id"] == "window-1"
    assert metrics(report)["latency_mean_ms"]["current"]["observed_count"] == 30
    recovered = compare_drift(reference, window(3))
    assert recovered["status"] == "within_bounds"
    assert reference.to_dict() == original
    original["window"]["cohorts"].clear()
    assert reference.to_dict()["window"]["cohorts"]
    with pytest.raises(FrozenInstanceError):
        reference._json = "changed"
    assert ReviewedBaseline.from_dict(reference.to_dict()).sha256 == reference.sha256


def test_distribution_shift_does_not_become_quality_loss():
    reference = baseline(
        rules=(
            DriftRule("model_mix", absolute=0.2),
            DriftRule("quality:accuracy", "decrease", absolute=0.02),
        )
    )
    report = compare_drift(reference, window(2, model="model-b"))
    assert metrics(report)["model_mix"]["status"] == "alert"
    assert metrics(report)["model_mix"]["delta_interval"] == [1.0, 1.0]
    assert metrics(report)["quality:accuracy"]["status"] == "within_bounds"
    assert report["quality_evidence"]["current"]["status"] == "complete_for_declared_metrics"


def test_missing_quality_and_instrumentation_loss_are_explicit():
    reference = baseline(
        rules=(
            DriftRule("instrumentation_complete_rate", "decrease", absolute=0.01),
            DriftRule("quality:accuracy", "decrease", absolute=0.02),
            DriftRule("input_tokens_mean", absolute=1),
        )
    )
    report = compare_drift(reference, window(2, unknown=True, score=None))
    assert metrics(report)["instrumentation_complete_rate"]["status"] == "alert"
    assert metrics(report)["input_tokens_mean"]["status"] == "indeterminate"
    assert metrics(report)["quality:accuracy"]["current"]["observed_count"] == 0
    assert metrics(report)["quality:accuracy"]["status"] == "indeterminate"
    assert report["quality_evidence"]["current"]["status"] == "missing"


def test_sampling_change_is_incomparable_without_population_inflation():
    policy = RetentionPolicy(
        "sampling", "v1", mode="full", payloads="capture", protected_cohorts=("keep",)
    )
    reference = baseline(window(policy=policy))
    current = window(2, policy=replace(policy, sample_rate=0.2))
    report = compare_drift(reference, current)
    assert report["status"] == "indeterminate"
    assert all(item["status"] == "incomparable" for item in report["metrics"])
    assert metrics(report)["latency_mean_ms"]["current"]["total_count"] == 30


def test_small_samples_noise_margin_and_histogram_resolution_abstain():
    assert compare_drift(baseline(), window(2, count=3, latency=900))["status"] == "indeterminate"
    reference = baseline(rules=(DriftRule("latency_mean_ms", absolute=10, noise_margin=10),))
    assert compare_drift(reference, window(2, latency=105))["status"] == "indeterminate"
    builder = WindowBuilder(spec(2), quality_metrics=(QUALITY,))
    for index in range(30):
        builder.add(trace(index, day=2, latency=90 if index < 15 else 110), quality={"accuracy": 1})
    reference = baseline(rules=(DriftRule("latency_p95_ms", absolute=5),))
    report = compare_drift(reference, builder.to_dict())
    assert report["metrics"][0]["status"] == "indeterminate"
    assert report["metrics"][0]["reason"] == "quantile_resolution_or_reviewed_noise_margin"


def test_version_changes_and_window_order_are_not_silent():
    builder = WindowBuilder(replace(spec(2), model_version="model:v2"), quality_metrics=(QUALITY,))
    for index in range(30):
        builder.add(trace(index, day=2), quality={"accuracy": 1})
    report = compare_drift(baseline(), builder.to_dict())
    assert report["potential_confounders"] == ["model_version"]
    report = compare_drift(baseline(), window())
    assert all(item["reason"] == "overlapping_or_nonlater_window" for item in report["metrics"])
    with pytest.raises(ValueError, match="pinned hash"):
        compare_drift(baseline(), window(2), expected_baseline_sha256="wrong")


def test_cohorts_are_separate_and_new_cohorts_are_not_matched():
    current = WindowBuilder(spec(2), quality_metrics=(QUALITY,))
    for index in range(30):
        current.add(trace(index, day=2, latency=200), cohort="new", quality={"accuracy": 1})
    report = compare_drift(baseline(), current.to_dict())
    assert {item["cohort"] for item in report["metrics"]} == {"all", "new"}
    assert all(item["reason"] == "cohort_missing_from_one_window" for item in report["metrics"])


def test_timeout_abstention_and_failure_denominators():
    current = WindowBuilder(spec(2), quality_metrics=(QUALITY,))
    for index in range(30):
        current.add(trace(index, day=2, failed=index < 15), timeout=index < 3, abstention=index < 6)
    rules = tuple(
        DriftRule(metric, absolute=0.01)
        for metric in ("failure_rate", "timeout_rate", "abstention_rate")
    )
    report = compare_drift(baseline(rules=rules), current.to_dict())
    for name, value in (("failure_rate", 0.5), ("timeout_rate", 0.1), ("abstention_rate", 0.2)):
        assert metrics(report)[name]["current"]["interval"] == [value, value]
        assert metrics(report)[name]["status"] == "alert"


def test_aggregate_import_requires_assignment_and_leaves_extras_unknown():
    aggregate = TraceAggregate().consume(trace(index, day=2) for index in range(30))
    builder = WindowBuilder(spec(2), quality_metrics=(QUALITY,))
    builder.add_aggregate(aggregate, time_assignment_ref="fixture:window-2")
    current = builder.to_dict()
    assert current["cohorts"]["all"]["time_membership"]["host_assigned_aggregate_runs"] == 30
    report = compare_drift(baseline(), current)
    assert metrics(report)["latency_mean_ms"]["status"] == "within_bounds"
    assert metrics(report)["quality:accuracy"]["status"] == "indeterminate"
    assert validate_window(current) == current


def test_window_assignment_and_add_errors_are_atomic():
    builder = WindowBuilder(spec(), quality_metrics=(QUALITY,))
    before = builder.to_dict()
    with pytest.raises(ValueError, match="outside"):
        builder.add(trace(day=2))
    with pytest.raises(ValueError, match="quality"):
        builder.add(trace(), quality={"accuracy": 2})
    assert builder.to_dict() == before
    builder.add(trace(), quality={"accuracy": 1})
    changed = builder.to_dict()
    changed["cohorts"]["all"]["flags"]["timeout"]["known"] = 10
    with pytest.raises(ValueError, match="hash"):
        validate_window(changed)


def test_finding_incidence_is_optional_and_versioned():
    reference = baseline(
        window(include_findings=True), rules=(DriftRule("finding_incidence", absolute=0.1),)
    )
    report = compare_drift(reference, window(2, include_findings=True))
    assert report["metrics"][0]["status"] == "within_bounds"
    report = compare_drift(reference, window(2, include_findings=False))
    assert report["metrics"][0]["status"] == "incomparable"


def test_local_cli_collect_review_compare_never_overwrites(tmp_path):
    from dataclasses import asdict

    source = trace().export_json(tmp_path / "trace.json")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "spec": asdict(spec()),
                "quality_metrics": [asdict(QUALITY)],
                "records": [{"trace": source.name, "quality": {"accuracy": 1}}],
            }
        )
    )
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([asdict(DriftRule("latency_mean_ms", absolute=10))]))
    baseline_path, window_path, current_path, output = [
        tmp_path / name for name in ("baseline.json", "window.json", "current.json", "report.json")
    ]
    runner = CliRunner()
    result = runner.invoke(app, ["drift", "collect", str(manifest), "--out", str(window_path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "drift",
            "baseline",
            str(window_path),
            str(rules),
            "--review-ref",
            "fixture:v1",
            "--out",
            str(baseline_path),
        ],
    )
    assert result.exit_code == 0, result.output
    current_path.write_text(json.dumps(window(2)))
    pinned = json.loads(baseline_path.read_text())["sha256"]
    command = [
        "drift",
        "compare",
        str(baseline_path),
        str(current_path),
        "--baseline-sha256",
        pinned,
        "--out",
        str(output),
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["status"] == "indeterminate"  # one-run baseline
    before = output.read_bytes()
    assert runner.invoke(app, command).exit_code != 0
    assert output.read_bytes() == before


def test_proxy_definition_and_missing_unrequested_quality_are_reported():
    proxy = QualityMetric("format", "proxy", "v1", "fixture:schema-check")
    builders = [WindowBuilder(spec(day), quality_metrics=(proxy,)) for day in (1, 2)]
    for day, builder in enumerate(builders, 1):
        for index in range(30):
            builder.add(trace(index, day=day), quality={"format": 1})
    reference = baseline(
        builders[0].to_dict(), rules=(DriftRule("quality:format", "decrease", absolute=0.1),)
    )
    result = compare_drift(reference, builders[1].to_dict())
    assert result["metrics"][0]["current"]["kind"] == "proxy"
    assert (
        result["metrics"][0]["current"]["quality_definition"]["source_ref"]
        == "fixture:schema-check"
    )
    no_labels = WindowBuilder(spec(2), quality_metrics=(proxy,))
    for index in range(30):
        no_labels.add(trace(index, day=2))
    assert (
        compare_drift(reference, no_labels.to_dict())["quality_evidence"]["current"]["status"]
        == "missing"
    )


def test_distribution_overflow_is_indeterminate_and_state_bounded():
    builders = [WindowBuilder(spec(day), quality_metrics=(QUALITY,)) for day in (1, 2)]
    for day, builder in enumerate(builders, 1):
        for index in range(150):
            builder.add(trace(index, day=day, model=f"model-{index}"))
    reference = baseline(builders[0].to_dict(), rules=(DriftRule("model_mix", absolute=0.1),))
    current = builders[1].to_dict()
    assert len(current["cohorts"]["all"]["mixes"]["model"]["counts"]) == 128
    assert compare_drift(reference, current)["metrics"][0]["status"] == "indeterminate"


def test_measurement_basis_change_and_quality_version_change_are_incomparable():
    builder = WindowBuilder(spec(2), quality_metrics=(replace(QUALITY, version="v2"),))
    for index in range(30):
        source = trace(index, day=2)
        source.elapsed_ms = None
        builder.add(source, quality={"accuracy": 1})
    report = compare_drift(baseline(), builder.to_dict())
    assert metrics(report)["latency_mean_ms"]["reason"] == "runtime_measurement_basis_changed"
    assert metrics(report)["quality:accuracy"]["reason"] == "quality_definition_changed"


def test_decision_outcomes_and_relative_thresholds():
    builders = [WindowBuilder(spec(day), quality_metrics=(QUALITY,)) for day in (1, 2)]
    for day, builder in enumerate(builders, 1):
        for index in range(30):
            source = trace(index, day=day, latency=100 if day == 1 else 120)
            source.events[0].metadata.update(
                operation_metadata(
                    "classifier", stage=StageInfo("classify", "v1", kind="classifier")
                )
            )
            source.events[0].metadata["agentloop.stage"]["outcome"] = (
                "class-a" if day == 1 else "class-b"
            )
            builder.add(source, quality={"accuracy": 1})
    reference = baseline(
        builders[0].to_dict(),
        rules=(
            DriftRule("decision_outcome_mix", absolute=0.1),
            DriftRule("latency_mean_ms", relative_pct=10),
        ),
    )
    report = compare_drift(reference, builders[1].to_dict())
    assert metrics(report)["decision_outcome_mix"]["status"] == "alert"
    assert metrics(report)["latency_mean_ms"]["relative_delta_pct"] == 20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"absolute": -1},
        {"relative_pct": float("nan")},
        {"absolute": 1, "minimum_coverage": 0},
        {"absolute": 1, "minimum_observations": True},
    ],
)
def test_invalid_thresholds_rejected(kwargs):
    with pytest.raises(ValueError):
        DriftRule("latency_mean_ms", **kwargs)


def test_many_spans_from_one_run_do_not_satisfy_sample_floor():
    builders = [WindowBuilder(spec(day)) for day in (1, 2)]
    for day, builder in enumerate(builders, 1):
        source = trace(day=day, model=f"model-{day}")
        first = source.events[0]
        source.events = []
        for index in range(30):
            event = deepcopy(first)
            event.event_id = str(index)
            source.add_event(event)
        builder.add(source)
    reference = baseline(builders[0].to_dict(), rules=(DriftRule("model_mix", absolute=0.1),))
    result = compare_drift(reference, builders[1].to_dict())["metrics"][0]
    assert result["current"]["observed_count"] == 30
    assert result["current"]["run_count"] == 1
    assert result["status"] == "indeterminate"
