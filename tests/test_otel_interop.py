from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.findings import build_diagnosis
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call
from agentloop.version import __version__


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
