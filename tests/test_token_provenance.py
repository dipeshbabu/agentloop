"""Token-count provenance and completeness semantics (issue #133).

These cover the contract that a token number carries where it came from, that a
word-count approximation never reads as exact usage, and that a cost comparison
built on approximated counts does not gate.
"""

from __future__ import annotations

import json

import pytest

from agentloop.costs import format_cost_usd
from agentloop.events import AgentEvent
from agentloop.metrics import token_breakdown
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.schema import SCHEMA_VERSION, TraceValidationError
from agentloop.tokens import (
    ESTIMATED_WORDS,
    PROVIDER,
    UNAVAILABLE,
    USER_SUPPLIED,
    TokenProvenanceError,
    is_token_basis_evaluable,
    is_token_basis_exact,
    provenance_grade,
    token_status,
    validate_provenance,
)
from agentloop.tracer import AgentTrace, record_model_call, trace_agent, trace_model_call


def _model_event(provenance: str | None, *, input_tokens: int = 10) -> AgentEvent:
    return AgentEvent(
        event_id="evt_1",
        run_id="run_1",
        event_type="model_call",
        name="call",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        duration_ms=1000.0,
        input_tokens=input_tokens,
        output_tokens=5,
        token_provenance=provenance,
    )


# --- recording provenance -------------------------------------------------


def test_explicit_counts_are_user_supplied() -> None:
    with trace_agent("explicit") as trace:
        with trace_model_call("call", input_tokens=100, output_tokens=20):
            pass
    event = trace.events[0]
    assert event.token_provenance == USER_SUPPLIED
    assert (event.input_tokens, event.output_tokens) == (100, 20)
    assert trace.report()["token_status"] == "exact"


def test_word_fallback_is_marked_estimated() -> None:
    with trace_agent("estimated") as trace:
        with trace_model_call("call", input_text="one two three four", output_tokens=5):
            pass
    event = trace.events[0]
    assert event.token_provenance == ESTIMATED_WORDS
    assert event.input_tokens == 4
    assert trace.report()["token_status"] == "estimated"


def test_one_estimated_side_downgrades_the_whole_event() -> None:
    """A total mixing a real count with an approximation is an approximation."""

    with trace_agent("mixed-sides") as trace:
        with trace_model_call("call", input_tokens=100, output_text="a b c"):
            pass
    assert trace.events[0].token_provenance == ESTIMATED_WORDS


def test_no_counts_and_no_text_is_unavailable_not_a_measured_zero() -> None:
    with trace_agent("nothing") as trace:
        with trace_model_call("call"):
            pass
    event = trace.events[0]
    assert event.token_provenance == UNAVAILABLE
    assert (event.input_tokens, event.output_tokens) == (0, 0)
    assert trace.report()["token_status"] == "unavailable"


def test_empty_text_does_not_count_as_an_estimate() -> None:
    with trace_agent("empty-text") as trace:
        with trace_model_call("call", input_text="", output_text=""):
            pass
    assert trace.events[0].token_provenance == UNAVAILABLE


def test_record_model_call_accepts_provider_provenance() -> None:
    trace = AgentTrace("provider")
    record_model_call(
        "call",
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=1.0,
        input_tokens=11,
        output_tokens=3,
        token_provenance=PROVIDER,
        trace=trace,
    )
    assert trace.events[0].token_provenance == PROVIDER
    assert trace.report()["token_status"] == "exact"


def test_record_model_call_infers_from_counts_when_provenance_omitted() -> None:
    trace = AgentTrace("inferred")
    record_model_call("counted", started_at="t", duration_ms=1.0, input_tokens=5, trace=trace)
    record_model_call("uncounted", started_at="t", duration_ms=1.0, trace=trace)
    assert trace.events[0].token_provenance == USER_SUPPLIED
    assert trace.events[1].token_provenance == UNAVAILABLE


def test_producer_rejects_an_undefined_provenance() -> None:
    trace = AgentTrace("bad")
    with pytest.raises(TokenProvenanceError):
        record_model_call(
            "call", started_at="t", duration_ms=1.0, token_provenance="vibes", trace=trace
        )


def test_validate_provenance_rejects_the_read_only_unspecified_value() -> None:
    """``unspecified`` is a read result for legacy traces, never something to write."""

    with pytest.raises(TokenProvenanceError):
        validate_provenance("unspecified")


# --- aggregate status -----------------------------------------------------


@pytest.mark.parametrize(
    ("provenances", "expected"),
    [
        ([], "empty"),
        ([PROVIDER], "exact"),
        ([PROVIDER, USER_SUPPLIED], "exact"),
        ([PROVIDER, ESTIMATED_WORDS], "partial"),
        ([PROVIDER, None], "partial"),
        ([ESTIMATED_WORDS], "estimated"),
        ([ESTIMATED_WORDS, UNAVAILABLE], "estimated"),
        ([ESTIMATED_WORDS, None], "estimated"),
        ([UNAVAILABLE], "unavailable"),
        ([None], "unspecified"),
        ([None, UNAVAILABLE], "unspecified"),
    ],
)
def test_token_status_aggregation(provenances: list[str | None], expected: str) -> None:
    assert token_status([_model_event(p) for p in provenances]) == expected


def test_unrecognized_provenance_grades_as_unspecified_not_exact() -> None:
    """Forward compatibility: a newer value must not be claimed to be exact."""

    assert provenance_grade("some_future_tokenizer_v9") == "unspecified"
    assert token_status([_model_event("some_future_tokenizer_v9")]) == "unspecified"


def test_basis_helpers_separate_exactness_from_gateability() -> None:
    assert is_token_basis_exact("exact") and is_token_basis_exact("empty")
    assert not is_token_basis_exact("unspecified")
    # Legacy traces still gate; a positively-declared estimate does not.
    assert is_token_basis_evaluable("unspecified")
    assert not is_token_basis_evaluable("estimated")


def test_token_breakdown_reports_counts_behind_the_status() -> None:
    events = [_model_event(PROVIDER), _model_event(ESTIMATED_WORDS), _model_event(None)]
    breakdown = token_breakdown(events)
    assert breakdown["token_status"] == "partial"
    assert breakdown["exact_token_call_count"] == 1
    assert breakdown["estimated_token_call_count"] == 1
    assert breakdown["token_provenance_counts"] == {
        ESTIMATED_WORDS: 1,
        PROVIDER: 1,
        "unspecified": 1,
    }


def test_report_exposes_token_status_next_to_the_totals() -> None:
    with trace_agent("mixed") as trace:
        with trace_model_call("counted", input_tokens=100, output_tokens=10):
            pass
        with trace_model_call("guessed", input_text="a b c d e", output_tokens=2):
            pass
    report = trace.report()
    assert report["token_status"] == "partial"
    assert report["input_tokens"] == 105
    assert report["token_provenance_counts"] == {ESTIMATED_WORDS: 1, USER_SUPPLIED: 1}


def test_cost_breakdown_carries_the_token_basis_behind_the_dollar_amount() -> None:
    with trace_agent("cost") as trace:
        with trace_model_call("call", model="gpt-4.1", input_text="a b c", output_tokens=5):
            pass
    cost = trace.report()["cost_breakdown"]
    # The cost is calculable, but it multiplies a rate by an approximation.
    assert cost["cost_status"] == "complete"
    assert cost["token_status"] == "estimated"


# --- serialization & compatibility ----------------------------------------


def test_provenance_survives_a_native_json_round_trip(tmp_path) -> None:
    with trace_agent("round-trip") as trace:
        with trace_model_call("call", input_tokens=7, output_tokens=2):
            pass
    path = tmp_path / "trace.json"
    trace.export_json(path)
    restored = AgentTrace.from_dict(json.loads(path.read_text()))
    assert restored.events[0].token_provenance == USER_SUPPLIED


def test_serialized_traces_declare_the_1_1_schema() -> None:
    with trace_agent("versioned") as trace:
        with trace_model_call("call", input_tokens=1):
            pass
    payload = trace.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION == "1.1"
    assert payload["events"][0]["token_provenance"] == USER_SUPPLIED


def test_legacy_trace_without_provenance_reads_back_as_unspecified() -> None:
    """A 1.0 trace stays readable, and its counts are not claimed to be exact."""

    legacy = {
        "schema_version": "1.0",
        "name": "legacy",
        "run_id": "run_legacy",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "elapsed_ms": 1000.0,
        "metadata": {},
        "events": [
            {
                "event_id": "evt_1",
                "run_id": "run_legacy",
                "event_type": "model_call",
                "name": "plan",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 1000.0,
                "input_tokens": 10,
                "output_tokens": 5,
                "status": "ok",
                "metadata": {},
            }
        ],
    }
    trace = AgentTrace.from_dict(legacy)
    assert trace.events[0].token_provenance is None
    assert trace.report()["token_status"] == "unspecified"


def test_unknown_provenance_value_does_not_make_a_trace_unreadable() -> None:
    """MINOR forward compatibility: reject nothing, claim nothing."""

    payload = {
        "schema_version": "1.2",
        "name": "future",
        "run_id": "run_future",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "elapsed_ms": 1000.0,
        "metadata": {},
        "events": [
            {
                "event_id": "evt_1",
                "run_id": "run_future",
                "event_type": "model_call",
                "name": "plan",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 1000.0,
                "input_tokens": 10,
                "output_tokens": 5,
                "token_provenance": "harmonic_tokenizer",
                "status": "ok",
                "metadata": {},
            }
        ],
    }
    trace = AgentTrace.from_dict(payload)
    assert trace.events[0].token_provenance == "harmonic_tokenizer"
    assert trace.report()["token_status"] == "unspecified"


def test_non_string_provenance_is_rejected_on_read() -> None:
    with pytest.raises(TraceValidationError) as excinfo:
        AgentEvent.from_dict(
            {
                "event_id": "evt_1",
                "run_id": "run_1",
                "event_type": "model_call",
                "name": "plan",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 1.0,
                "token_provenance": 7,
            }
        )
    assert excinfo.value.field == "events[0].token_provenance"


# --- OTLP interop ---------------------------------------------------------


def test_provenance_survives_an_otlp_round_trip() -> None:
    with trace_agent("otlp") as trace:
        with trace_model_call("call", input_tokens=9, output_tokens=4):
            pass
    restored = trace_from_otel(trace_to_otel(trace))
    assert restored.events[0].token_provenance == USER_SUPPLIED
    # The transport attribute must not leak into user metadata.
    assert "agentloop.token_provenance" not in restored.events[0].metadata


def test_estimated_provenance_survives_an_otlp_round_trip() -> None:
    with trace_agent("otlp-estimated") as trace:
        with trace_model_call("call", input_text="a b c d", output_tokens=1):
            pass
    restored = trace_from_otel(trace_to_otel(trace))
    assert restored.events[0].token_provenance == ESTIMATED_WORDS
    assert restored.report()["token_status"] == "estimated"


def test_third_party_span_with_usage_is_provider_reported() -> None:
    payload = {
        "spans": [
            {
                "traceId": "a" * 32,
                "spanId": "b" * 16,
                "name": "chat",
                "startTimeUnixNano": "0",
                "endTimeUnixNano": "1000000",
                "attributes": [
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 120}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 30}},
                ],
            }
        ]
    }
    trace = trace_from_otel(payload)
    assert trace.events[0].token_provenance == PROVIDER
    assert trace.report()["token_status"] == "exact"


def test_third_party_span_without_usage_is_unavailable() -> None:
    payload = {
        "spans": [
            {
                "traceId": "a" * 32,
                "spanId": "b" * 16,
                "name": "chat",
                "startTimeUnixNano": "0",
                "endTimeUnixNano": "1000000",
                "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}],
            }
        ]
    }
    trace = trace_from_otel(payload)
    assert trace.events[0].token_provenance == UNAVAILABLE
    assert trace.report()["token_status"] == "unavailable"


# --- cost gates -----------------------------------------------------------


def _fixed_trace(name: str) -> AgentTrace:
    """A trace with pinned timing, so gate comparisons never read a wall clock."""

    return AgentTrace(
        name,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        elapsed_ms=1000.0,
    )


def _priced_trace(name: str, *, input_tokens: int, estimated: bool) -> AgentTrace:
    """Build a one-call priced trace with either counted or approximated tokens."""

    trace = _fixed_trace(name)
    record_model_call(
        "call",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        duration_ms=1000.0,
        model="gpt-4.1",
        input_tokens=input_tokens,
        output_tokens=10,
        token_provenance=ESTIMATED_WORDS if estimated else PROVIDER,
        trace=trace,
    )
    trace.finish()
    return trace


def test_cost_gates_are_indeterminate_when_tokens_are_estimated() -> None:
    baseline = _priced_trace("baseline", input_tokens=1000, estimated=True)
    candidate = _priced_trace("candidate", input_tokens=500, estimated=True)
    report = build_replay_report(baseline, candidate, gates=ReplayGates(min_cost_improvement_pct=5))
    gates = report["gates"]
    assert gates["pricing_known"] is True
    assert gates["token_basis_evaluable"] is False
    assert gates["cost_evaluable"] is False
    assert "cost_improvement" in gates["indeterminate"]
    # A required improvement that cannot be verified must not silently pass.
    assert gates["passed"] is False
    assert report["deltas"]["cost_improvement_pct"] is None
    detail = next(g["detail"] for g in gates["results"] if g["name"] == "cost_regression")
    assert "approximated from word counts" in detail


def test_estimated_tokens_do_not_fail_a_run_that_requires_no_cost_improvement() -> None:
    baseline = _priced_trace("baseline", input_tokens=1000, estimated=True)
    candidate = _priced_trace("candidate", input_tokens=500, estimated=True)
    report = build_replay_report(baseline, candidate, gates=ReplayGates())
    gates = report["gates"]
    assert gates["cost_evaluable"] is False
    assert gates["passed"] is True
    assert "token counts are estimates" in report["summary"]


def test_cost_gates_evaluate_when_tokens_are_provider_reported() -> None:
    baseline = _priced_trace("baseline", input_tokens=1000, estimated=False)
    candidate = _priced_trace("candidate", input_tokens=500, estimated=False)
    report = build_replay_report(baseline, candidate, gates=ReplayGates(min_cost_improvement_pct=5))
    gates = report["gates"]
    assert gates["token_basis_evaluable"] is True
    assert gates["cost_evaluable"] is True
    assert gates["passed"] is True
    assert report["deltas"]["cost_improvement_pct"] > 0


def test_legacy_traces_still_gate_so_upgrades_do_not_break_ci() -> None:
    """Documented compatibility path: unknown provenance keeps gating."""

    def legacy(name: str, tokens: int) -> AgentTrace:
        trace = _fixed_trace(name)
        trace.add_event(
            AgentEvent(
                event_id=f"evt_{name}",
                run_id=trace.run_id,
                event_type="model_call",
                name="call",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:01+00:00",
                duration_ms=1000.0,
                model="gpt-4.1",
                input_tokens=tokens,
                output_tokens=10,
            )
        )
        trace.finish()
        return trace

    report = build_replay_report(
        legacy("baseline", 1000),
        legacy("candidate", 500),
        gates=ReplayGates(min_cost_improvement_pct=5),
    )
    gates = report["gates"]
    assert gates["baseline_token_status"] == "unspecified"
    assert gates["token_basis_evaluable"] is True
    assert gates["cost_evaluable"] is True
    assert gates["passed"] is True


def test_a_mixed_basis_run_does_not_gate_on_cost() -> None:
    baseline = _priced_trace("baseline", input_tokens=1000, estimated=False)
    candidate = _priced_trace("candidate", input_tokens=500, estimated=True)
    gates = build_replay_report(baseline, candidate, gates=ReplayGates())["gates"]
    assert gates["baseline_token_status"] == "exact"
    assert gates["candidate_token_status"] == "estimated"
    assert gates["cost_evaluable"] is False


# --- cost rendering -------------------------------------------------------


def test_format_cost_qualifies_an_estimated_token_basis() -> None:
    assert format_cost_usd(0.0004, "complete", token_status="estimated") == (
        "$0.0004 (from estimated tokens)"
    )
    assert format_cost_usd(0.0004, "complete", token_status="unspecified") == (
        "$0.0004 (from unspecified tokens)"
    )


def test_format_cost_leaves_an_exact_basis_unqualified() -> None:
    assert format_cost_usd(0.0004, "complete", token_status="exact") == "$0.0004"
    assert format_cost_usd(0.0004, "complete", token_status="empty") == "$0.0004"
    # Omitting the basis keeps the pre-existing pricing-only rendering.
    assert format_cost_usd(0.0004, "complete") == "$0.0004"


def test_format_cost_reports_both_pricing_and_token_caveats() -> None:
    rendered = format_cost_usd(0.0004, "partial", token_status="estimated")
    assert rendered == "$0.0004 (known lower bound) (from estimated tokens)"


def test_printed_report_states_the_token_basis(capsys) -> None:
    with trace_agent("printed") as trace:
        with trace_model_call("call", model="gpt-4.1", input_text="a b c", output_tokens=5):
            pass
    trace.print_report()
    out = capsys.readouterr().out
    assert "(from estimated tokens)" in out
    assert "Token basis: approximated from whitespace word counts" in out
