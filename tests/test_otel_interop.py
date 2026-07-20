from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.findings import build_diagnosis
from agentloop.otel import trace_from_otel, trace_to_otel, traces_from_otel
from agentloop.schema import TraceValidationError
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call
from agentloop.version import __version__


def _span(trace_id: str, span_id: str, *, start: str = "1", end: str = "2") -> dict:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
    }


def test_trace_from_otel_imports_genai_spans() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "0" * 31 + "1",
                                "spanId": "a" * 16,
                                "name": "plan",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1400000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "gpt-test"},
                                    },
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": 100},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": 20},
                                    },
                                ],
                            },
                            {
                                "traceId": "0" * 31 + "1",
                                "spanId": "b" * 16,
                                "parentSpanId": "a" * 16,
                                "name": "search_web tool",
                                "startTimeUnixNano": "1400000000",
                                "endTimeUnixNano": "1700000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "execute_tool"},
                                    },
                                    {
                                        "key": "gen_ai.tool.name",
                                        "value": {"stringValue": "search_web"},
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }

    trace = trace_from_otel(payload, name="imported")

    assert trace.name == "imported"
    # The full 32-char trace id is preserved (not truncated to 16) so a valid
    # OTLP id round-trips unchanged.
    assert trace.run_id == "run_" + "0" * 31 + "1"
    assert len(trace.events) == 2
    assert trace.events[0].event_type == "model_call"
    assert trace.events[0].model == "gpt-test"
    assert trace.events[0].input_tokens == 100
    assert trace.events[1].event_type == "tool_call"
    assert trace.events[1].parent_id == "span_" + "a" * 16


def test_trace_to_otel_round_trips_native_trace() -> None:
    with trace_agent("roundtrip") as trace:
        with trace_model_call("plan", model="gpt-test", input_tokens=10, output_tokens=5):
            pass
        with trace_tool_call("search"):
            pass

    payload = trace_to_otel(trace)
    assert payload["resourceSpans"][0]["scopeSpans"][0]["scope"]["version"] == __version__
    imported = trace_from_otel(payload)

    assert len(imported.events) == 2
    assert imported.events[0].event_type == "model_call"
    assert imported.events[0].input_tokens == 10
    assert imported.events[1].event_type == "tool_call"


def test_build_diagnosis_returns_machine_actionable_findings(tmp_path) -> None:
    with trace_agent("diagnosis") as trace:
        for _ in range(3):
            with trace_tool_call("read_source"):
                pass

    diagnosis = build_diagnosis(trace)

    assert diagnosis["summary"]["finding_count"] >= 1
    finding = diagnosis["findings"][0]
    assert finding["finding_id"].startswith("al_")
    assert finding["rewrite"]["patchable"] is True
    assert finding["validation"]["command"] == "agentloop replay"


def test_cli_diagnose_and_otel_commands(tmp_path) -> None:
    with trace_agent("cli") as trace:
        for _ in range(3):
            with trace_tool_call("read_source"):
                pass
    native = tmp_path / "trace.json"
    trace.export_json(native)

    runner = CliRunner()
    diagnosis_md = tmp_path / "diagnosis.md"
    diagnosis_json = tmp_path / "diagnosis.json"
    result = runner.invoke(
        app,
        [
            "diagnose",
            "--path",
            str(native),
            "--out",
            str(diagnosis_md),
            "--json-out",
            str(diagnosis_json),
        ],
    )
    assert result.exit_code == 0
    assert diagnosis_md.exists()
    assert json.loads(diagnosis_json.read_text(encoding="utf-8"))["summary"]["finding_count"] >= 1

    otel_path = tmp_path / "trace.otel.json"
    result = runner.invoke(app, ["export-otel", str(native), str(otel_path)])
    assert result.exit_code == 0
    assert otel_path.exists()

    imported = tmp_path / "imported.json"
    result = runner.invoke(
        app, ["import-otel", str(otel_path), str(imported), "--name", "imported"]
    )
    assert result.exit_code == 0
    assert json.loads(imported.read_text(encoding="utf-8"))["name"] == "imported"


# --- #40: multi-trace batch boundaries --------------------------------------


def test_traces_from_otel_splits_a_batch_by_trace_id() -> None:
    payload = {
        "spans": [
            _span("a" * 32, "1" * 16),
            _span("b" * 32, "2" * 16),
        ]
    }

    traces = traces_from_otel(payload)

    assert len(traces) == 2
    run_ids = {trace.run_id for trace in traces}
    assert run_ids == {"run_" + "a" * 32, "run_" + "b" * 32}
    for trace in traces:
        assert len(trace.events) == 1
        assert trace.events[0].run_id == trace.run_id


def test_traces_from_otel_groups_interleaved_spans_regardless_of_order() -> None:
    payload = {
        "spans": [
            _span("a" * 32, "1" * 16),
            _span("b" * 32, "2" * 16),
            _span("a" * 32, "3" * 16),
            _span("b" * 32, "4" * 16),
        ]
    }

    traces = {trace.run_id: trace for trace in traces_from_otel(payload)}

    assert set(traces) == {"run_" + "a" * 32, "run_" + "b" * 32}
    assert len(traces["run_" + "a" * 32].events) == 2
    assert len(traces["run_" + "b" * 32].events) == 2


def test_repeated_span_id_in_different_traces_stays_separated() -> None:
    payload = [
        _span("a" * 32, "dup"),
        _span("b" * 32, "dup"),
    ]

    traces = traces_from_otel(payload)

    assert len(traces) == 2
    # Parent links cannot cross a trace boundary: each trace keeps its own span.
    assert {t.run_id for t in traces} == {"run_" + "a" * 32, "run_" + "b" * 32}


def test_trace_from_otel_rejects_a_multi_trace_batch() -> None:
    payload = {"spans": [_span("a" * 32, "1" * 16), _span("b" * 32, "2" * 16)]}

    with pytest.raises(TraceValidationError) as excinfo:
        trace_from_otel(payload)

    assert excinfo.value.field == "traceId"
    assert "traces_from_otel" in excinfo.value.reason


def test_single_trace_payload_stays_backward_compatible() -> None:
    payload = {"spans": [_span("a" * 32, "1" * 16), _span("a" * 32, "2" * 16)]}

    trace = trace_from_otel(payload)

    assert trace.run_id == "run_" + "a" * 32
    assert len(trace.events) == 2
    assert traces_from_otel(payload) == [trace] or len(traces_from_otel(payload)) == 1


def test_spans_without_trace_ids_form_one_trace() -> None:
    payload = [
        {"spanId": "1" * 16, "startTimeUnixNano": "1", "endTimeUnixNano": "2"},
        {"spanId": "2" * 16, "startTimeUnixNano": "3", "endTimeUnixNano": "4"},
    ]

    trace = trace_from_otel(payload)

    assert trace.run_id == "run_otel_import"
    assert len(trace.events) == 2


# --- #63: native identity and metadata across round trips -------------------


def test_native_identity_survives_repeated_round_trips() -> None:
    with trace_agent("native-name", metadata={"tenant": "acme"}) as trace:
        with trace_model_call("plan", model="gpt-test", metadata={"region": "us"}):
            pass
        with trace_tool_call("search", metadata={"tool": "web"}):
            pass
    original_run_id = trace.run_id
    original_ids = [event.event_id for event in trace.events]
    original_parents = [event.parent_id for event in trace.events]

    once = trace_from_otel(trace_to_otel(trace))
    twice = trace_from_otel(trace_to_otel(once))

    for imported in (once, twice):
        assert imported.name == "native-name"
        assert imported.run_id == original_run_id
        assert [e.event_id for e in imported.events] == original_ids
        assert [e.parent_id for e in imported.events] == original_parents
        # agentloop.metadata.region decodes exactly once, without prefix growth.
        assert imported.events[0].metadata["region"] == "us"
        assert imported.events[1].metadata["tool"] == "web"
        assert not any(
            key.startswith("agentloop.metadata.")
            for event in imported.events
            for key in event.metadata
        )


def test_transport_attributes_do_not_leak_into_user_metadata() -> None:
    with trace_agent("transport") as trace:
        with trace_model_call("plan"):
            pass

    imported = trace_from_otel(trace_to_otel(trace))
    round_tripped = trace_from_otel(trace_to_otel(imported))

    # otel_* transport keys stay constant instead of accumulating each round trip.
    for event in round_tripped.events:
        assert "agentloop.run_id" not in event.metadata
        assert "agentloop.native_event_id" not in event.metadata
        leaked = [k for k in event.metadata if k.startswith("agentloop.metadata.otel_")]
        assert leaked == []


def test_third_party_span_attributes_are_preserved() -> None:
    payload = {
        "spans": [
            {
                "traceId": "a" * 32,
                "spanId": "1" * 16,
                "startTimeUnixNano": "1",
                "endTimeUnixNano": "2",
                "attributes": [
                    {"key": "vendor.custom", "value": {"stringValue": "keep-me"}},
                ],
            }
        ]
    }

    trace = trace_from_otel(payload)

    assert trace.events[0].metadata["vendor.custom"] == "keep-me"
