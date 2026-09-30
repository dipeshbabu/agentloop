from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.replay import build_replay_report
from agentloop.retention import RetentionContext, RetentionPolicy, RetentionSession, read_retention
from agentloop.retention_types import RETENTION_KEY
from agentloop.runtime import CLEAR, FinalizationError, finalize_trace, init, reset_runtime
from agentloop.store import SQLiteTraceStore
from agentloop.studies import StudyValidationError, _run
from agentloop.tracer import AgentTrace

STAMP = "2026-09-01T00:00:00+00:00"
END = "2026-09-01T00:00:01+00:00"
SUCCESS = RetentionContext(outcome="success")


def trace(run_id="run-one", count=3):
    result = AgentTrace(
        "private-name",
        run_id=run_id,
        started_at=STAMP,
        ended_at=END,
        elapsed_ms=1000,
        metadata={"private": "secret-metadata"},
    )
    for index in range(count):
        result.add_event(
            AgentEvent(
                event_id=f"event-{index}",
                run_id=run_id,
                event_type="model_call",
                name="private-operation",
                model="private-model",
                started_at=STAMP,
                ended_at=END,
                duration_ms=10,
                parent_id=f"event-{index - 1}" if index else None,
                input_tokens=100,
                output_tokens=10,
                token_provenance="provider",
                input_text="secret-input",
                output_text="secret-output",
                metadata={"provider_reported_cost_usd": 0.25, "private": "secret-event"},
            )
        )
    return result


def policy(**kwargs):
    return RetentionPolicy("test-retention", "1", **kwargs)


@pytest.mark.parametrize("mode", ["full", "compact", "metrics_only"])
def test_modes_preserve_identity_original_metrics_and_private_payload_omission(mode):
    original = trace()
    before = original.to_dict()
    retained = RetentionSession(policy(mode=mode, max_events=1)).retain(original, context=SUCCESS)
    assert original.to_dict() == before
    assert retained.run_id == original.run_id
    report = retained.report()
    expected = original.report()
    for key in (
        "event_count",
        "model_call_count",
        "total_runtime_ms",
        "cumulative_span_time_ms",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "token_status",
        "cost_status",
        "repeated_context_ratio",
    ):
        assert report[key] == expected[key]
    assert report["analysis_complete"] is False
    assert not report["finding_candidates"]
    assert report["retained_event_count"] == {"full": 3, "compact": 1, "metrics_only": 0}[mode]
    serialized = json.dumps(retained.to_dict())
    assert "secret-" not in serialized
    assert "private-" not in serialized
    restored = AgentTrace.from_dict(json.loads(serialized))
    assert restored.report() == report
    assert read_retention(restored)["sampling"]["effective_rate"] == 1


def test_explicit_full_capture_keeps_complete_analysis_and_replay():
    original = trace()
    retained = RetentionSession(policy(mode="full", payloads="capture")).retain(
        original, context=SUCCESS
    )
    report = retained.report()
    report.pop("retention")
    assert report == original.report()
    assert retained.events[0].input_text == "secret-input"
    assert read_retention(retained)["complete_evidence"] is True
    build_replay_report(retained, original)
    retained.events[0].metadata["private"] = "changed"
    assert original.events[0].metadata["private"] == "secret-event"


@pytest.mark.parametrize("sampling", ["deterministic", "probabilistic"])
def test_seeded_sampling_reproducible_and_counts_explicit(sampling):
    config = policy(sampling=sampling, sample_rate=0.25)

    def collect():
        session = RetentionSession(config)
        kept = [index for index in range(80) if session.retain(trace(str(index)), context=SUCCESS)]
        return kept, session.summary()

    first, summary = collect()
    assert collect() == (first, summary)
    assert 5 < len(first) < 40
    assert summary["observed_input_count"] == 80
    assert summary["observed_sample_count"] == len(first)
    assert summary["estimated_population_count"] is None
    assert summary["effective_rate"] == len(first) / 80


def test_deterministic_keys_are_order_independent_and_clustered():
    config = policy(sample_rate=0.4)
    forward, backward = RetentionSession(config), RetentionSession(config)
    expected = {
        str(i): forward.retain(trace(str(i)), context=SUCCESS) is not None for i in range(30)
    }
    actual = {
        str(i): backward.retain(trace(str(i)), context=SUCCESS) is not None
        for i in reversed(range(30))
    }
    assert actual == expected
    session = RetentionSession(config)
    results = [
        session.retain(trace(str(i)), context=replace(SUCCESS, sample_key="one-cluster"))
        is not None
        for i in range(5)
    ]
    assert len(set(results)) == 1


@pytest.mark.parametrize(
    "context,extra,reason",
    [
        (RetentionContext(outcome="failure"), {}, "failure_or_timeout"),
        (RetentionContext(outcome="timeout"), {}, "failure_or_timeout"),
        (RetentionContext(), {}, "unknown"),
        (replace(SUCCESS, disagreement=True), {}, "disagreement"),
        (replace(SUCCESS, anomaly=True), {}, "anomaly"),
        (
            replace(SUCCESS, cohorts=("protected",)),
            {"protected_cohorts": ("protected",)},
            "protected_cohort",
        ),
        (SUCCESS, {"high_cost_usd": 0.7}, "high_cost"),
        (SUCCESS, {"high_latency_ms": 999}, "high_latency"),
    ],
)
def test_protected_reasons_override_zero_sampling(context, extra, reason):
    session = RetentionSession(policy(sample_rate=0, **extra))
    retained = session.retain(trace(), context=context)
    evidence = read_retention(retained)
    assert reason in evidence["reasons"]
    assert evidence["conditional_inclusion_rate"] == 1


def test_unknown_usage_and_errors_retained_without_payloads():
    source = trace(count=5)
    source.events[-1].status = "error"
    source.events[-1].error = "secret-failure"
    source.events[2].token_provenance = None
    source.events[2].metadata = {}
    retained = RetentionSession(policy(sample_rate=0, max_events=1)).retain(source, context=SUCCESS)
    evidence = read_retention(retained)
    assert evidence["reasons"] == ["failure_or_timeout", "unknown"]
    assert evidence["retained_original_indices"] == [0, 4]
    assert [event.event_id for event in retained.events] == ["event-0", "event-4"]
    assert retained.events[-1].parent_id == "event-3"  # No invented parent or reorder.
    assert retained.events[-1].error is None
    assert retained.report()["error_span_count"] == 1
    assert retained.report()["cost_status"] == "partial"


def test_representative_bucket_limit_and_first_k_are_explicit():
    session = RetentionSession(policy(sample_rate=0, representatives_per_bucket=2, max_buckets=2))
    assert session.retain(trace("a"), context=SUCCESS)
    assert session.retain(trace("b"), context=SUCCESS)
    assert session.retain(trace("c"), context=SUCCESS) is None
    for i in range(15):
        source = trace(str(i), count=1)
        source.events[0].name = str(i)
        session.retain(source, context=SUCCESS)
    assert session.summary()["tracked_bucket_count"] == 2
    assert session.summary()["retention_reason_counts"]["representative_bucket"] == 3


def test_redactor_is_explicit_versioned_and_only_sees_retained_text():
    calls = []

    def redact(value):
        calls.append(value)
        return "[redacted]"

    session = RetentionSession(
        policy(payloads="redact", redactor_ref="redactor:v1", max_events=1), redactor=redact
    )
    retained = session.retain(trace(), context=SUCCESS)
    assert calls == ["secret-input", "secret-output"]
    assert retained.events[0].input_text == "[redacted]"
    assert retained.events[0].metadata["retention_payloads"]["input_text"]["length"] == 12
    assert "secret-" not in json.dumps(retained.to_dict())
    assert read_retention(retained)["policy"]["redactor_ref"] == "redactor:v1"


@pytest.mark.parametrize("mutation", ["metrics", "events", "run_id", "version"])
def test_mutated_evidence_fails_closed(mutation):
    retained = RetentionSession(policy()).retain(trace(), context=SUCCESS)
    if mutation == "metrics":
        retained.metadata[RETENTION_KEY]["metrics"]["input_tokens"] = 0
    elif mutation == "events":
        retained.events.reverse()
    elif mutation == "run_id":
        retained.run_id = "different"
    else:
        retained.metadata[RETENTION_KEY]["schema_version"] = "99"
    with pytest.raises(ValueError, match="retention"):
        retained.report()


def test_lossy_evidence_rejected_by_replay_and_study_and_roundtrips_store(tmp_path):
    retained = RetentionSession(policy(mode="metrics_only")).retain(trace(), context=SUCCESS)
    path = retained.export_json(tmp_path / "trace.json")
    with pytest.raises(ValueError, match="requires complete trace evidence"):
        build_replay_report(trace(), retained)
    with pytest.raises(StudyValidationError, match="retention omitted"):
        _run(path, [])
    diagnosis = build_diagnosis(retained)
    assert diagnosis["analysis_complete"] is False
    assert "missing evidence" in diagnosis_to_markdown(diagnosis)
    store = SQLiteTraceStore(str(tmp_path / "store.db"))
    store.init()
    store.save_trace(retained)


def test_runtime_retains_before_any_destination_and_can_clear(tmp_path):
    reset_runtime()
    try:
        session = RetentionSession(policy(mode="metrics_only"))
        init(export_dir=tmp_path, retention=session)
        result = finalize_trace(trace())
        saved = AgentTrace.from_json(result["exported_path"])
        assert not saved.events
        assert saved.report()["input_tokens"] == 300
        assert result["retention"]["retained"]
        assert init(retention=None).retention is session
        assert init(retention=CLEAR).retention is None
    finally:
        reset_runtime()


def test_runtime_sampling_and_redaction_failure_never_export_raw(tmp_path):
    reset_runtime()
    try:
        init(
            export_dir=tmp_path,
            retention=RetentionSession(policy(sample_rate=0, retain_unknowns=False)),
        )
        assert finalize_trace(trace())["retention"]["retained"] is False
        assert not list(tmp_path.iterdir())

        def fail(value):
            raise RuntimeError("secret-error-detail")

        session = RetentionSession(
            policy(payloads="redact", redactor_ref="fails:v1"), redactor=fail
        )
        init(retention=session)
        result = finalize_trace(trace())
        assert result["errors"][0]["destination"] == "retention"
        assert "secret" not in json.dumps(result)
        assert session.summary()["observed_input_count"] == 0
        assert not list(tmp_path.iterdir())
        init(fail_silently=False)
        with pytest.raises(FinalizationError):
            finalize_trace(trace())
    finally:
        reset_runtime()


def test_session_serializes_counts_and_owns_reports():
    session = RetentionSession(policy(mode="metrics_only"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        traces = list(pool.map(lambda i: session.retain(trace(str(i)), context=SUCCESS), range(20)))
    assert session.summary()["observed_input_count"] == 20
    assert sorted(
        read_retention(item)["sampling"]["observed_sample_count"] for item in traces
    ) == list(range(1, 21))
    report = traces[0].report()
    report["retention"]["metrics"]["input_tokens"] = 0
    assert traces[0].report()["input_tokens"] == 300


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": float("nan")},
        {"sample_rate": -1},
        {"sample_rate": True},
        {"sample_rate": 2},
        {"max_events": True},
        {"max_buckets": 0},
        {"high_cost_usd": float("inf")},
        {"payloads": "redact"},
        {"protected_cohorts": ["x"]},
        {"retain_unknowns": 1},
    ],
)
def test_invalid_policies_rejected(kwargs):
    with pytest.raises(ValueError):
        policy(**kwargs)


def test_unfinished_and_double_retention_rejected():
    session = RetentionSession(policy())
    with pytest.raises(ValueError, match="finished"):
        session.retain(AgentTrace("unfinished"))
    with pytest.raises(ValueError, match="already retained"):
        session.retain(session.retain(trace()))


def test_stage_order_schema_summaries_and_workflow_failure_survive():
    from agentloop.workflow_types import (
        StageInfo,
        WorkflowInfo,
        operation_metadata,
        workflow_metadata,
    )

    source = trace()
    source.metadata = workflow_metadata(WorkflowInfo("private-workflow", "v1"), status="failed")
    source.events[1].metadata.update(
        operation_metadata(
            "model", stage=StageInfo("private-stage", "v1", input_schema_ref="private-schema")
        )
    )
    retained = RetentionSession(policy(sample_rate=0)).retain(source)
    assert "failure_or_timeout" in read_retention(retained)["reasons"]
    assert read_retention(retained)["retained_original_indices"] == [0, 1, 2]
    stage = retained.events[1].metadata["retention_stage"]
    assert stage["schema_status"] == "supported"
    assert len(stage["input_schema_ref_sha256"]) == 64
    assert "private-" not in json.dumps(retained.to_dict())


def test_large_metric_only_artifact_skips_graph_analysis(monkeypatch):
    import agentloop.metrics as metrics

    def unexpected(*args, **kwargs):
        pytest.fail("retention should not run graph or finding analysis")

    monkeypatch.setattr(metrics, "parallelism_opportunities", unexpected)
    monkeypatch.setattr(metrics, "run_rules", unexpected)
    source = trace(count=2000)
    retained = RetentionSession(policy(mode="metrics_only")).retain(source, context=SUCCESS)
    assert retained.report()["event_count"] == 2000
    assert len(json.dumps(retained.to_dict())) < 10_000


@pytest.mark.parametrize("result", [7, None, "x" * 100_001], ids=["number", "null", "oversize"])
def test_invalid_redactor_result_and_configuration_are_rejected(result):
    with pytest.raises(ValueError, match="explicit trusted redactor"):
        RetentionSession(policy(payloads="redact", redactor_ref="v1"))
    with pytest.raises(ValueError, match="explicit trusted redactor"):
        RetentionSession(policy(), redactor=lambda text: text)
    session = RetentionSession(
        policy(payloads="redact", redactor_ref="v1"), redactor=lambda text: result
    )
    with pytest.raises(ValueError, match="redactor must return"):
        session.retain(trace())
    assert session.summary()["observed_sample_count"] == 0


def test_redactor_source_mutation_rejected_without_committing_session():
    source = trace()

    def mutate(text):
        source.events[0].input_tokens += 1
        return "redacted"

    session = RetentionSession(policy(payloads="redact", redactor_ref="v1"), redactor=mutate)
    with pytest.raises(ValueError, match="source trace changed"):
        session.retain(source)
    assert session.summary()["observed_input_count"] == 0
