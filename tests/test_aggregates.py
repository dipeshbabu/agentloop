from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from agentloop.aggregate_stats import AggregateConfig, WeightedCandidates
from agentloop.aggregates import TraceAggregate, aggregate_files
from agentloop.cli import app
from agentloop.costs import ModelPricing, PricingTable
from agentloop.events import AgentEvent
from agentloop.retention import RetentionContext, RetentionPolicy, RetentionSession
from agentloop.tracer import AgentTrace
from agentloop.workflow_types import StageInfo, operation_metadata


def trace(index=0, *, unknown=False, failed=False):
    source = AgentTrace(
        "test",
        run_id=f"run-{index}",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:00:01Z",
        elapsed_ms=index + 1,
        metadata={"success": not failed},
    )
    metadata = operation_metadata("model", stage=StageInfo("stage", "v1", kind="model"))
    if not unknown:
        metadata["provider_reported_cost_usd"] = 0.25
    source.add_event(
        AgentEvent(
            "evt",
            source.run_id,
            "model_call",
            "model",
            source.started_at,
            source.ended_at,
            1,
            model="unpriced",
            input_tokens=10,
            output_tokens=2,
            token_provenance=None if unknown else "provider",
            status="error" if failed else "ok",
            metadata=metadata,
        )
    )
    return source


def test_incremental_exact_counts_and_partition_roundtrip():
    sources = [trace(index, failed=index == 3) for index in range(10)]
    whole = TraceAggregate().consume(iter(sources))
    left = TraceAggregate().consume(iter(sources[:4]))
    right = TraceAggregate().consume(iter(sources[4:]))
    merged = TraceAggregate.from_dict(left.to_dict()).merge(
        TraceAggregate.from_dict(right.to_dict())
    )
    assert merged.to_dict() == whole.to_dict()
    report = merged.to_dict()
    assert report["counts"]["runs"] == 10
    assert report["counts"]["stage_spans"] == 10
    assert report["categories"]["outcomes"] == {"success": 9, "failure": 1, "unknown": 0}
    assert report["completeness"]["input_tokens"] == 100
    assert report["cost"]["provider_reported_usd"] == 2.5
    assert report["latency"]["sum_ms"] == 55
    assert report["latency"]["mean_ms"] == 5.5
    for quantile, expected in (("0.5", 5), ("0.9", 9), ("0.99", 10)):
        bounds = report["latency"]["quantile_intervals"][quantile]
        assert bounds["lower_ms"] <= expected <= bounds["upper_ms"]
    assert report["analysis"]["findings_available"] is False


def test_unknown_usage_and_cost_never_read_as_zero():
    report = TraceAggregate().consume([trace(), trace(1, unknown=True)]).to_dict()
    assert report["counts"]["input_tokens"] == 20
    assert report["completeness"]["input_tokens"] is None
    assert report["completeness"]["known_model_cost_usd"] == 0.25
    assert report["completeness"]["pricing_complete_model_cost_usd"] is None
    assert report["categories"]["token_provenance"]["unspecified"] == 1
    assert report["categories"]["cost_calls"]["unknown"] == 1
    assert report["counts"]["unknown_cost_runs"] == 1


def test_cost_sources_and_estimated_token_basis_remain_distinct():
    pricing = PricingTable({"priced": ModelPricing(1, 2, "test", "test:v1", "2026-09-01")})
    source = trace()
    source.events[0].model = "priced"
    source.events[0].metadata.pop("provider_reported_cost_usd")
    source.events[0].token_provenance = "estimated_words"
    report = TraceAggregate(pricing=pricing).consume([source]).to_dict()
    assert report["cost"]["calculated_usd"] == 0.000014
    assert report["categories"]["cost_calls"]["calculated"] == 1
    assert report["completeness"]["input_tokens"] is None
    assert report["counts"]["inexact_token_runs"] == 1


@pytest.mark.parametrize(
    "mode,payloads", [("metrics_only", "omit"), ("compact", "omit"), ("full", "capture")]
)
def test_retained_measurements_and_sampling_denominators(mode, payloads):
    session = RetentionSession(
        RetentionPolicy(
            "test", "v1", mode=mode, payloads=payloads, sample_rate=0, protected_cohorts=("test",)
        )
    )
    retained = [
        session.retain(trace(i), context=RetentionContext(outcome="success", cohorts=("test",)))
        for i in range(3)
    ]
    report = TraceAggregate().consume(retained).to_dict()
    assert report["counts"]["events"] == 3
    assert report["semantics"]["retention_policy"]["sample_rate"] == 0
    assert report["sampling"]["observed_record_count"] == 3
    assert report["sampling"]["input_population_count"] is None
    assert report["sampling"]["estimated_population_count"] is None
    assert report["counts"]["stage_evidence_missing_runs"] == (0 if mode == "full" else 3)
    assert report["categories"]["cost_calls"]["priced_unsplit"] == 3
    assert report["completeness"]["stage_cost_missing_runs"] == 3
    assert TraceAggregate.from_dict(report).to_dict() == report


@pytest.mark.parametrize("difference", ["config", "sampling", "pricing", "raw"])
def test_incompatible_merges_rejected_atomically(difference):
    left = TraceAggregate().consume([trace()])
    right = TraceAggregate(AggregateConfig(heavy_hitter_capacity=3)).consume([trace()])
    if difference == "pricing":
        right = TraceAggregate(pricing=PricingTable()).consume([trace()])
    elif difference in {"sampling", "raw"}:
        session = RetentionSession(RetentionPolicy("retained", "v1"))
        retained = session.retain(trace())
        right = TraceAggregate().consume([retained])
        if difference == "sampling":
            other = RetentionSession(replace(session.policy, sample_rate=0.5))
            left = TraceAggregate().consume([other.retain(trace())])
    before = left.to_dict()
    with pytest.raises(ValueError, match="incompatible"):
        left.merge(right)
    assert left.to_dict() == before


def test_empty_partitions_unknown_outcome_and_atomic_input_error():
    empty = TraceAggregate()
    assert TraceAggregate.from_dict(empty.to_dict()).to_dict() == empty.to_dict()
    source = trace()
    source.metadata = {}
    populated = TraceAggregate().consume([source])
    before = populated.to_dict()
    assert populated.merge(empty).to_dict() == before
    assert empty.merge(populated).to_dict() == before
    assert before["categories"]["outcomes"]["unknown"] == 1
    invalid = trace()
    invalid.events[0].input_tokens = -1
    with pytest.raises(ValueError):
        populated.add(invalid)
    assert populated.to_dict() == before
    populated.consume([invalid, source], on_error="skip")
    assert populated.to_dict()["counts"]["invalid_inputs"] == 1


def test_bounded_candidates_and_error_intervals_survive_merge():
    exact = Counter()
    left, right = WeightedCandidates(3), WeightedCandidates(3)
    for i in range(1200):
        key, weight = ("heavy", 50) if i % 4 == 0 else (f"key-{i}", i % 7 + 1)
        exact[key] += weight
        (left if i % 2 else right).add(key, weight)
    left.merge(right)
    assert len(left.weights) <= 3
    assert left.weights["heavy"] > 0
    for key, actual in exact.items():
        lower = left.weights.get(key, 0)
        assert lower <= actual <= lower + left.error
    assert left.total == sum(exact.values())


def test_state_bounded_and_stream_does_not_collect_inputs(monkeypatch):
    import agentloop.metrics as metrics

    def forbidden(*args, **kwargs):
        pytest.fail("aggregate must not build full reports")

    monkeypatch.setattr(metrics, "build_report", forbidden)
    aggregate = TraceAggregate(AggregateConfig(heavy_hitter_capacity=4))
    for i in range(1000):
        source = trace(i)
        source.events[0].metadata.update(
            operation_metadata("model", stage=StageInfo(str(i), "v1", kind="model"))
        )
        aggregate.add(source)
    report = aggregate.to_dict()
    assert report["counts"]["runs"] == 1000
    assert len(json.dumps(report)) < 20_000
    assert all(len(sketch["candidates"]) <= 4 for sketch in report["heavy_hitters"].values())


def test_decision_buckets_and_missing_stage_evidence():
    source = trace()
    event = source.events[0]
    event.event_type = "tool_call"
    event.metadata = operation_metadata(
        "classifier", stage=StageInfo("classify", "v1", kind="classifier")
    )
    event.metadata["agentloop.stage"]["outcome"] = "approve"
    report = TraceAggregate().consume([source]).to_dict()
    assert report["counts"]["decision_spans"] == 1
    assert report["categories"]["operations"]["classifier"] == 1
    assert report["heavy_hitters"]["decision_outcomes"]["total_weight"] == 1
    assert "approve" not in json.dumps(report)


def test_artifact_changes_and_unsupported_version_rejected():
    original = TraceAggregate().consume([trace()]).to_dict()
    for field, value in (("schema_version", "2.0"), ("counts", {}), ("sha256", "wrong")):
        modified = copy.deepcopy(original)
        modified[field] = value
        with pytest.raises(ValueError):
            TraceAggregate.from_dict(modified)


def test_multi_file_cli_manifest_partial_failures_and_merge(tmp_path):
    first = trace().export_json(tmp_path / "first.json")
    second = trace(1).export_json(tmp_path / "second.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json")
    result = aggregate_files(iter([first, invalid, second]), on_error="skip").to_dict()
    assert result["counts"]["runs"] == 2
    assert result["counts"]["invalid_inputs"] == 1
    manifest = tmp_path / "inputs.jsonl"
    manifest.write_text('"first.json"\n"second.json"\n', encoding="utf-8")
    out = tmp_path / "part.json"
    runner = CliRunner()
    response = runner.invoke(
        app, ["aggregate", "traces", "--manifest", str(manifest), "--out", str(out)]
    )
    assert response.exit_code == 0, response.output
    merged = tmp_path / "merged.json"
    response = runner.invoke(app, ["aggregate", "merge", str(out), "--out", str(merged)])
    assert response.exit_code == 0, response.output
    assert json.loads(merged.read_text()) == json.loads(out.read_text())
    assert (
        runner.invoke(app, ["aggregate", "traces", str(first), "--out", str(first)]).exit_code != 0
    )
    assert (
        runner.invoke(
            app, ["aggregate", "traces", str(invalid), "--out", str(tmp_path / "no.json")]
        ).exit_code
        != 0
    )
    assert not (tmp_path / "no.json").exists()


def test_input_byte_limit_and_empty_stream(tmp_path):
    path = trace().export_json(tmp_path / "source.json")
    with pytest.raises(ValueError, match="byte limit"):
        aggregate_files([path], max_bytes=10)
    report = aggregate_files(iter(())).to_dict()
    assert report["latency"]["quantile_intervals"]["0.5"] is None
    assert report["sampling"]["observed_record_count"] == 0


def test_invalid_native_event_cannot_be_hidden_in_metric_snapshot():
    source = trace()
    source.events[0].input_tokens = -1
    with pytest.raises(ValueError):
        RetentionSession(RetentionPolicy("test", "v1", mode="metrics_only")).retain(source)


def test_overflow_rejected_without_losing_previous_partition():
    source = trace()
    source.elapsed_ms = 1e308
    aggregate = TraceAggregate().consume([source])
    before = aggregate.to_dict()
    with pytest.raises(ValueError, match="finite"):
        aggregate.add(source)
    assert aggregate.to_dict() == before


def test_workflow_outcomes_override_host_labels_even_without_failure_protection():
    from agentloop.workflow_types import WorkflowInfo, workflow_metadata

    for status, expected in (
        ("failed", "failure"),
        ("running", "unknown"),
        ("completed", "success"),
    ):
        source = trace()
        source.metadata = workflow_metadata(
            WorkflowInfo("workflow", "v1"), status=status, metadata={"success": True}
        )
        report = TraceAggregate().consume([source]).to_dict()
        assert report["categories"]["outcomes"][expected] == 1
        retained = RetentionSession(
            RetentionPolicy("test", "v1", mode="metrics_only", retain_failures=False)
        ).retain(source, context=RetentionContext(outcome="success"))
        report = TraceAggregate().consume([retained]).to_dict()
        assert report["categories"]["outcomes"][expected] == 1


def test_legacy_task_failure_survives_sampling_and_aggregation():
    source = trace()
    source.metadata["success"] = False
    retained = RetentionSession(
        RetentionPolicy("test", "v1", sample_rate=0, retain_unknowns=False, mode="metrics_only")
    ).retain(source)
    assert retained is not None
    report = TraceAggregate().consume([retained]).to_dict()
    assert report["categories"]["outcomes"]["failure"] == 1
