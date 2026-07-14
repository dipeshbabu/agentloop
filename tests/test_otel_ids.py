from __future__ import annotations

from agentloop.events import AgentEvent
from agentloop.otel import trace_to_otel
from agentloop.otel_ids import (
    is_valid_span_id,
    is_valid_trace_id,
    to_span_id,
    to_trace_id,
)
from agentloop.tracer import AgentTrace

# --- validity of produced ids ------------------------------------------------

CUSTOM_TRACE_IDS = [
    "customer/run",           # path-like / non-hex
    "run_customer/run",       # prefixed non-hex
    "café-trace-ünïcode",     # unicode
    "abc",                    # short
    "z" * 40,                 # long, non-hex
    "0" * 32,                 # all-zero (invalid per OTLP)
    "",                       # empty
    "run_" + "a" * 16,        # native-style (uuid4().hex[:16])
]

CUSTOM_SPAN_IDS = [
    "event/nothex",
    "evt_event/nothex",
    "span_" + "b" * 16,
    "0" * 16,
    "",
    "λ-span",
]


def test_to_trace_id_is_always_valid() -> None:
    for native in CUSTOM_TRACE_IDS:
        out = to_trace_id(native)
        assert is_valid_trace_id(out), (native, out)


def test_to_span_id_is_always_valid() -> None:
    for native in CUSTOM_SPAN_IDS:
        out = to_span_id(native)
        assert is_valid_span_id(out), (native, out)


# --- already-valid ids are preserved ----------------------------------------

def test_valid_trace_id_round_trips_unchanged() -> None:
    valid = "abcdef0123456789abcdef0123456789"
    assert is_valid_trace_id(valid)
    assert to_trace_id(valid) == valid
    assert to_trace_id("run_" + valid) == valid  # prefix stripped, rest preserved


def test_valid_span_id_round_trips_unchanged() -> None:
    valid = "0123456789abcdef"
    assert is_valid_span_id(valid)
    assert to_span_id(valid) == valid
    assert to_span_id("span_" + valid) == valid


def test_uppercase_hex_is_normalized_not_hashed() -> None:
    # A valid hex id in uppercase is lowercased (still the "same" id), not hashed.
    assert to_trace_id("ABCDEF0123456789ABCDEF0123456789") == "abcdef0123456789abcdef0123456789"


# --- determinism and collision resistance -----------------------------------

def test_mapping_is_deterministic() -> None:
    for native in CUSTOM_TRACE_IDS:
        assert to_trace_id(native) == to_trace_id(native)


def test_distinct_native_ids_do_not_collide() -> None:
    outs = [to_trace_id(n) for n in CUSTOM_TRACE_IDS]
    assert len(set(outs)) == len(outs)
    span_outs = [to_span_id(n) for n in CUSTOM_SPAN_IDS]
    assert len(set(span_outs)) == len(span_outs)


def test_all_zero_is_remapped_to_nonzero() -> None:
    assert set(to_trace_id("0" * 32)) != {"0"}
    assert set(to_span_id("0" * 16)) != {"0"}


# --- export end-to-end -------------------------------------------------------

def test_export_emits_valid_ids_for_custom_run_id() -> None:
    # A user-chosen, non-hex run id and event ids (as the API/local constructors
    # allow) must still export as valid OTLP identifiers.
    trace = AgentTrace(name="custom", run_id="customer/run")
    trace.add_event(
        AgentEvent(
            event_id="event/nothex",
            run_id="customer/run",
            event_type="model_call",
            name="plan",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            duration_ms=1000.0,
            model="m",
        )
    )
    trace.add_event(
        AgentEvent(
            event_id="child/span",
            run_id="customer/run",
            event_type="tool_call",
            name="search",
            started_at="2026-01-01T00:00:01+00:00",
            ended_at="2026-01-01T00:00:02+00:00",
            duration_ms=1000.0,
            parent_id="event/nothex",
        )
    )

    payload = trace_to_otel(trace)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans
    for span in spans:
        assert is_valid_trace_id(span["traceId"]), span["traceId"]
        assert is_valid_span_id(span["spanId"]), span["spanId"]
        if "parentSpanId" in span:
            assert is_valid_span_id(span["parentSpanId"]), span["parentSpanId"]
        # The original native ids survive in attributes for diagnosability.
        keys = {a["key"] for a in span["attributes"]}
        assert "agentloop.native_event_id" in keys
        assert "agentloop.run_id" in keys
