from __future__ import annotations

import asyncio
import copy
import json

import pytest

from agentloop.entrypoint import _analysis_payload, _quickstart_trace
from agentloop.harness import (
    Decision,
    Harness,
    HarnessConfig,
    HarnessDeniedError,
    HarnessEscalationError,
    Hook,
    Policy,
)
from agentloop.harness_evidence import (
    HarnessDecisionRecord,
    HarnessEvidenceError,
    append_records,
    read_evidence,
    validate_evidence,
)
from agentloop.html_report import analysis_to_html
from agentloop.interventions import InterventionValidationError, build_intervention
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates
from agentloop.tracer import AgentTrace, bind_trace_context, record_model_call, trace_agent


def guard(action="deny", *, version="1", configuration=None, callback=None):
    return Policy(
        "guard",
        version,
        callback or (lambda context: Decision(action, "reviewed_policy")),
        hooks={Hook("tool")},
        actions={"continue", "deny", "stop", "escalate"},
        configuration=configuration or {},
    )


def capture(mode="shadow", *, entry=None, capture_configuration=False):
    with trace_agent("evidence", metadata={"synthetic": True}) as trace:
        run = Harness(
            HarnessConfig(
                mode,
                (entry or guard(),),
                capture_policy_configuration=capture_configuration,
            )
        ).start_run()
        try:
            run.wrap(lambda: "ok", boundary="tool")()
        except HarnessDeniedError:
            pass
    return trace, run


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_decisions_round_trip_through_native_json_and_otlp_without_usage_promotion(mode):
    trace, run = capture(mode)
    original = read_evidence(trace)
    assert original == run.export_evidence()
    assert original == validate_evidence(json.loads(json.dumps(original)))
    native = AgentTrace.from_dict(json.loads(json.dumps(trace.to_dict())))
    imported = trace_from_otel(trace_to_otel(trace))
    assert read_evidence(native) == read_evidence(imported) == original
    record = next(item for item in original["decisions"].values() if item["origin"] == "policy")
    assert record["outcome"] == ("proposed" if mode == "shadow" else "applied")
    assert record["budget_snapshot"] is None and record["evaluation_status"] == "unverified"
    assert imported.report()["model_call_count"] == imported.report()["tool_call_count"] == 0


def test_disabled_harness_does_not_add_evidence_to_tracing():
    trace, run = capture("disabled")
    assert "agentloop.harness" not in trace.metadata
    assert run.export_evidence()["decisions"] == {}


def test_conflicts_failures_and_no_ops_remain_distinct_with_one_effect_per_hook():
    def fail(context):
        raise ValueError("private policy failure")

    policies = (
        Policy("continue", "1", lambda context: Decision(), actions={"continue"}),
        Policy("deny", "1", lambda context: Decision("deny"), actions={"deny"}),
        Policy("escalate", "1", lambda context: Decision("escalate"), actions={"escalate"}),
        Policy("error", "1", fail, actions={"continue"}),
    )
    with trace_agent("conflicts") as trace:
        run = Harness(HarnessConfig("enforce", policies)).start_run()
        with pytest.raises(HarnessEscalationError):
            run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    envelope = read_evidence(trace)
    before = {
        record["policy_id"]: record
        for record in envelope["decisions"].values()
        if record["phase"] == "before"
    }
    assert {key: value["outcome"] for key, value in before.items()} == {
        "continue": "no_op",
        "deny": "rejected",
        "error": "failed",
        "escalate": "applied",
    }
    assert len({record["hook_id"] for record in before.values()}) == 1
    assert before["escalate"]["decision_id"] in before["deny"]["conflicting_decision_ids"]
    assert "private policy failure" not in json.dumps(envelope)


def test_explicit_retry_links_use_prior_identity_and_do_not_repeat_a_callable():
    calls = []

    def admission(context):
        previous = context.state.get("last")
        context.state["last"] = context.decision_id
        return Decision(
            "continue", "host_retry", evidence_refs=("evidence-one",), retry_of=previous
        )

    with trace_agent("retries") as trace:
        run = Harness(HarnessConfig("enforce", (guard(callback=admission),))).start_run()
        target = run.wrap(lambda: calls.append(True), boundary="tool")
        target()
        target()  # The host, not the harness, requested the second attempt.
    records = [
        record
        for record in read_evidence(trace)["decisions"].values()
        if record["origin"] == "policy"
    ]
    assert len(calls) == 2 and len(records) == 2
    first = next(record for record in records if record["retry_of"] is None)
    second = next(record for record in records if record["retry_of"] is not None)
    assert second["retry_of"] == first["decision_id"]
    assert first["hook_sequence"] < second["hook_sequence"]
    assert second["evidence_refs"] == ["evidence-one"]
    assert len({record["decision_id"] for record in records}) == 2


@pytest.mark.parametrize("cancel_policy", [False, True])
def test_cancellation_records_keep_dispatch_provenance(cancel_policy):
    cancelled = asyncio.CancelledError("private cancellation")

    def policy(context):
        if cancel_policy:
            raise cancelled
        return Decision()

    def target():
        raise cancelled

    with trace_agent("cancelled") as trace:
        run = Harness(HarnessConfig("enforce", (guard(callback=policy),))).start_run()
        with pytest.raises(asyncio.CancelledError) as caught:
            run.wrap(target, boundary="tool")()
    assert caught.value is cancelled
    after = next(
        record
        for record in read_evidence(trace)["decisions"].values()
        if record["phase"] == "after"
    )
    assert after["execution_status"] == "cancelled"
    assert after["dispatched"] is (not cancel_policy)
    assert "private cancellation" not in json.dumps(read_evidence(trace))


def test_snapshots_are_historical_and_raw_configuration_capture_is_explicit():
    configuration = {"credential": "private-credential", "nested": {"limit": 2}}
    entry = guard(configuration=configuration)
    hidden, _ = capture(entry=entry)
    visible, run = capture(entry=entry, capture_configuration=True)
    configuration["nested"]["limit"] = 999
    hidden_evidence = read_evidence(hidden)
    visible_evidence = read_evidence(visible)
    assert "private-credential" not in json.dumps(hidden_evidence)
    assert visible_evidence["policies"][entry.config_hash]["configuration"]["nested"]["limit"] == 2
    visible_evidence["policies"].clear()
    assert run.export_evidence()["policies"][entry.config_hash]["version"] == "1"
    assert "private-credential" not in analysis_to_html(_analysis_payload(visible))
    assert "private-credential" in analysis_to_html(
        _analysis_payload(visible), include_content=True
    )


def test_raw_inputs_outputs_and_policy_state_are_not_automatically_captured():
    secret = "private-prompt-output-and-state"

    def observe(context):
        context.state["private"] = secret
        return Decision()

    with trace_agent("privacy") as trace:
        run = Harness(HarnessConfig("enforce", (guard(callback=observe),))).start_run()
        assert run.wrap(lambda text: text, boundary="tool")(secret) == secret
    assert secret not in json.dumps(trace.to_dict())


def test_duplicate_writes_are_idempotent_and_conflicting_evidence_is_rejected():
    trace, _ = capture()
    evidence = read_evidence(trace)
    original = copy.deepcopy(evidence)
    records = tuple(
        HarnessDecisionRecord.from_dict(item) for item in evidence["decisions"].values()
    )
    append_records(evidence, records, evidence["policies"])
    assert evidence == original
    changed = records[0].to_dict()
    changed["reason_code"] = "different_reason"
    with pytest.raises(HarnessEvidenceError, match="different evidence"):
        append_records(evidence, (HarnessDecisionRecord.from_dict(changed),), evidence["policies"])
    assert evidence == original


def test_hook_timing_is_separate_from_model_usage_and_not_multiplied_per_policy(monkeypatch):
    ticks = iter([0, 1, 3, 4, 7, 9, 10, 12])
    monkeypatch.setattr("agentloop.harness.perf_counter_ns", lambda: next(ticks) * 1_000_000)
    monkeypatch.setattr("agentloop.harness.utc_now_iso", lambda: "2026-01-01T00:00:00+00:00")
    policies = tuple(
        Policy(name, "1", lambda context: Decision(), hooks={Hook("tool")}) for name in ("a", "b")
    )
    with trace_agent("timing", metadata={"synthetic": True}) as trace:
        record_model_call(
            "model",
            model="gpt-4o-mini",
            input_tokens=10,
            output_tokens=3,
            duration_ms=25,
            started_at="2026-01-01T00:00:00+00:00",
            trace=trace,
        )
        run = Harness(HarnessConfig("shadow", policies)).start_run()
        run.wrap(lambda: None, boundary="tool")()
    records = list(read_evidence(trace)["decisions"].values())
    assert {record["policy_id"]: record["timing"]["duration_ms"] for record in records} == {
        "a": 2,
        "b": 3,
        "agentloop.harness": 0,
    }
    report = trace.report()
    assert report["model_call_count"] == 1 and report["tool_call_count"] == 0
    assert report["model_time_ms"] == 25 and report["input_tokens"] == 10
    html = analysis_to_html(_analysis_payload(trace))
    assert "11.000 ms across 2 hooks" in html


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_capture_failures_are_observable_and_shadow_execution_is_unchanged(mode):
    invoked = []
    with trace_agent(
        "collision", metadata={"agentloop.harness": {"private": "secret-invalid-content"}}
    ) as trace:
        run = Harness(HarnessConfig(mode, (guard("continue"),))).start_run()
        target = run.wrap(lambda: invoked.append(True), boundary="tool")
        if mode == "enforce":
            with pytest.raises(HarnessEvidenceError):
                target()
        else:
            target()
    assert invoked == ([True] if mode == "shadow" else [])
    assert run.export_evidence()["capture_errors"]
    assert "secret-invalid-content" not in json.dumps(run.export_evidence())
    html = analysis_to_html(_analysis_payload(trace))
    assert "invalid or unsupported" in html and "secret-invalid-content" not in html


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_intervention_links_only_an_actual_pair_and_preserves_original_snapshots(mode):
    baseline = _quickstart_trace()
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    entry = guard(version="historical", configuration={"reviewed": True})
    with bind_trace_context(candidate):
        run = Harness(HarnessConfig(mode, (entry,))).start_run()
        try:
            run.wrap(lambda: None, boundary="tool")()
        except HarnessDeniedError:
            pass
    target = _analysis_payload(baseline)["diagnosis"]["findings"][0]["finding_id"]
    record = build_intervention(
        baseline, candidate, target_finding_ids=[target], intervention_type="guarded_tool"
    )
    evidence = record.to_dict()["metadata"]["agentloop.harness_evidence"]
    assert evidence["comparison_status"] == "observed_pair"
    assert evidence["individual_policy_effect"] == "unverified"
    assert len(evidence["applied_candidate_decision_ids"]) == (1 if mode == "enforce" else 0)
    candidate.metadata["agentloop.harness"]["policies"].clear()
    saved = record.to_dict()["metadata"]["agentloop.harness_evidence"]
    assert saved["candidate"]["policies"][entry.config_hash]["version"] == "historical"
    assert record.to_dict()["predicted"]["findings"][0]["estimate"]["calibrated"] is False


def test_callers_cannot_supply_derived_comparison_evidence():
    baseline = _quickstart_trace()
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    target = _analysis_payload(baseline)["diagnosis"]["findings"][0]["finding_id"]
    with pytest.raises(InterventionValidationError, match="derived"):
        build_intervention(
            baseline,
            candidate,
            target_finding_ids=[target],
            intervention_type="guard",
            metadata={"agentloop.harness_evidence": {"invented": True}},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", []),
        ("outcome", "success"),
        ("dispatched", "true"),
        ("decision_id", "forged"),
        ("evaluation_status", "verified"),
    ],
)
def test_malformed_decisions_are_rejected_without_echoing_the_payload(field, value):
    trace, _ = capture()
    payload = next(iter(read_evidence(trace)["decisions"].values()))
    payload[field] = value
    with pytest.raises(HarnessEvidenceError):
        HarnessDecisionRecord.from_dict(payload)


def test_unknown_evidence_versions_remain_transportable_but_are_not_rendered_as_controls():
    trace, _ = capture()
    trace.metadata["agentloop.harness"]["schema_version"] = "2.0"
    trace.metadata["agentloop.harness"]["future_private_field"] = "private-future-content"
    native = AgentTrace.from_dict(json.loads(json.dumps(trace.to_dict())))
    imported = trace_from_otel(trace_to_otel(native))
    assert imported.metadata["agentloop.harness"] == trace.metadata["agentloop.harness"]
    html = analysis_to_html(_analysis_payload(imported))
    assert "invalid or unsupported" in html and "private-future-content" not in html


def test_explicit_configuration_html_is_escaped_and_hidden_by_default():
    markup = '</pre><img src=x onerror="alert(1)"><script>alert(1)</script>'
    trace, _ = capture(entry=guard(configuration={"note": markup}), capture_configuration=True)
    hidden = analysis_to_html(_analysis_payload(trace))
    included = analysis_to_html(_analysis_payload(trace), include_content=True)
    assert "alert(1)" not in hidden
    assert "&lt;img" in included and "&lt;script&gt;" in included
    assert "<img" not in included and "<script>" not in included


def test_nested_protected_policy_work_is_not_counted_as_model_or_tool_usage():
    from agentloop import trace_tool

    run = None

    def recursive(context):
        @trace_tool()
        def tool():
            pytest.fail("nested policy work dispatched")

        run.wrap(tool, boundary="tool")()
        return Decision()

    with trace_agent("no-double-counting") as trace:
        run = Harness(HarnessConfig("enforce", (guard(callback=recursive),))).start_run()
        with pytest.raises(HarnessEscalationError):
            run.wrap(lambda: None, boundary="tool")()
    assert trace.report()["model_call_count"] == trace.report()["tool_call_count"] == 0
    assert any(
        record["outcome"] == "failed" for record in read_evidence(trace)["decisions"].values()
    )


def test_linked_evidence_retains_failed_quality_and_unknown_cost():
    baseline = _quickstart_trace()
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "failed-candidate"
    for trace in (baseline, candidate):
        for event in trace.events:
            event.run_id = trace.run_id
            if event.event_type == "model_call":
                event.model = "unpriced-example"
    with bind_trace_context(candidate):
        run = Harness(HarnessConfig("enforce", (guard(),))).start_run()
        with pytest.raises(HarnessDeniedError):
            run.wrap(lambda: None, boundary="tool")()
    target = _analysis_payload(baseline)["diagnosis"]["findings"][0]["finding_id"]
    quality = build_quality_report(
        [{"expected": "ok", "baseline_output": "ok", "candidate_output": "bad"}]
    )
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[target],
        intervention_type="guard",
        quality_report=quality,
        gates=ReplayGates(min_quality_score=1),
    ).to_dict()
    assert record["gates_passed"] is False
    assert record["measured"]["deltas"]["cost_usd_delta"] is None
    assert (
        record["metadata"]["agentloop.harness_evidence"]["individual_policy_effect"] == "unverified"
    )


def test_generator_decisions_stay_with_the_trace_that_admitted_the_stream():
    with trace_agent("owner") as owner:
        run = Harness(HarnessConfig("shadow")).start_run()

        def stream():
            yield 1

        protected = run.wrap(stream, boundary="tool")()
        assert next(protected) == 1
    with trace_agent("consumer") as consumer:
        protected.close()
    assert "agentloop.harness" not in consumer.metadata
    evidence = read_evidence(owner)
    assert {record["phase"] for record in evidence["decisions"].values()} == {"before", "after"}
    after = next(record for record in evidence["decisions"].values() if record["phase"] == "after")
    assert after["execution_status"] == "closed"


def test_legacy_application_harness_metadata_keeps_its_original_semantics():
    baseline = _quickstart_trace()
    baseline.metadata["harness"] = "legacy-runner"
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "legacy-candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    assert read_evidence(baseline) is None
    assert "legacy-runner" in analysis_to_html(_analysis_payload(baseline))
    target = _analysis_payload(baseline)["diagnosis"]["findings"][0]["finding_id"]
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[target],
        intervention_type="legacy",
        metadata={"harness_evidence": {"application_owned": True}},
    ).to_dict()
    assert record["metadata"] == {"harness_evidence": {"application_owned": True}}


@pytest.mark.parametrize(
    "failure", [ValueError("private failure"), asyncio.CancelledError("private cancellation")]
)
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_after_capture_failure_preserves_the_original_failure(failure, mode):
    with trace_agent("capture-after") as trace:
        run = Harness(HarnessConfig(mode)).start_run()

        def target():
            trace.metadata["agentloop.harness"] = None
            raise failure

        with pytest.raises(type(failure)) as caught:
            run.wrap(target, boundary="tool")()
    assert caught.value is failure
    assert run.stopped is (mode == "enforce")
    assert run.export_evidence()["capture_errors"]
    with pytest.raises(HarnessEvidenceError):
        read_evidence(trace)
    assert "private failure" not in json.dumps(run.export_evidence())
    assert "private cancellation" not in json.dumps(run.export_evidence())


def test_existing_opaque_trace_branch_and_evidence_identifiers_are_preserved():
    trace = AgentTrace("opaque", run_id="équipe/run 42")
    with bind_trace_context(trace, 'parent/span "42"'):
        entry = guard(
            callback=lambda context: Decision("continue", evidence_refs=(context.parent_span_id,))
        )
        run = Harness(HarnessConfig("shadow", (entry,))).start_run()
        run.wrap(lambda: None, boundary="tool", branch_id="tools/retrieve us-east")()
    evidence = read_evidence(trace)
    record = next(
        record for record in evidence["decisions"].values() if record["origin"] == "policy"
    )
    assert record["run_id"] == trace.run_id and record["branch_id"] == "tools/retrieve us-east"
    assert record["span_id"] == 'parent/span "42"'
    assert record["evidence_refs"] == ['parent/span "42"']
    assert read_evidence(trace_from_otel(trace_to_otel(trace))) == evidence


def test_missing_retry_evidence_is_retained_and_qualifies_the_comparison():
    baseline = _quickstart_trace()
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "partial-candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    missing = "hdec_" + "0" * 64
    with bind_trace_context(candidate):
        entry = guard(callback=lambda context: Decision("continue", retry_of=missing))
        run = Harness(HarnessConfig("shadow", (entry,))).start_run()
        run.wrap(lambda: None, boundary="tool")()
    target = _analysis_payload(baseline)["diagnosis"]["findings"][0]["finding_id"]
    record = build_intervention(
        baseline, candidate, target_finding_ids=[target], intervention_type="partial"
    ).to_dict()
    evidence = record["metadata"]["agentloop.harness_evidence"]
    assert evidence["capture_status"] == "known_incomplete"
    assert evidence["unresolved_decision_ids"] == [missing]
    assert evidence["applied_candidate_decision_ids"] == []
    assert "not retained" in analysis_to_html(_analysis_payload(candidate))
