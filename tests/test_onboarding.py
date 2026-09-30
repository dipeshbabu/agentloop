from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentloop import instrument
from agentloop.cli import app
from agentloop.entrypoint import _quickstart_trace
from agentloop.onboarding import onboard, validate_capture
from agentloop.retention import RetentionPolicy, RetentionSession
from agentloop.tracer import AgentTrace, trace_agent

FIXTURES = Path(__file__).parent / "fixtures" / "telemetry"


@pytest.mark.parametrize("fixture", ["genai", "openinference", "mcp"])
def test_existing_standard_telemetry_onboarding_keeps_boundaries_and_source(fixture):
    payload = json.loads((FIXTURES / f"{fixture}.json").read_text())["payload"]
    before = copy.deepcopy(payload)
    report = onboard(payload, format="otlp")
    assert payload == before
    assert report["trace_count"] == (2 if fixture == "genai" else 1)
    assert all(item["validation"]["event_count"] for item in report["traces"])
    assert all(item["analysis"] is not None for item in report["traces"])
    assert "metadata" not in report["traces"][0]


def test_missing_usage_operations_parentage_and_privacy_are_explicit():
    source = _quickstart_trace()
    source.events[0].parent_id = "missing"
    source.events[1].metadata["operation_kind"] = "unsupported-kind"
    source.events[2].error = "private-error"
    report = validate_capture(source, expected_operations=("model", "classifier"))
    codes = {item["code"] for item in report["checks"]}
    assert {
        "missing_parent",
        "unsupported_operation",
        "usage_not_exact",
        "payload_capture_present",
        "expected_operation_missing",
    } <= codes
    assert report["status"] == "invalid"
    assert report["analysis_allowed"] is False
    assert "private-error" not in json.dumps(report)
    summary = onboard(source.to_dict())
    assert summary["traces"][0]["analysis"] is None


def test_parent_cycle_and_late_stream_boundary_are_detected():
    source = _quickstart_trace()
    source.events[0].parent_id = source.events[1].event_id
    source.events[1].parent_id = source.events[0].event_id
    source.ended_at = source.started_at
    codes = {item["code"] for item in validate_capture(source)["checks"]}
    assert "parent_cycle" in codes
    assert "span_outside_boundary" in codes


def test_transport_boundary_ambiguity_is_not_analyzed():
    source = _quickstart_trace()
    source.events[0].metadata["otel_trace_id"] = None
    source.events[1].metadata["otel_trace_id"] = "a" * 32
    source.events[2].metadata["otel_trace_id"] = "b" * 32
    source.elapsed_ms = -1
    validation = validate_capture(source)
    codes = {item["code"] for item in validation["checks"]}
    assert {
        "transport_trace_id_missing",
        "transport_trace_ids_mixed",
        "invalid_elapsed_time",
    } <= codes
    assert validation["analysis_allowed"] is False


def test_empty_and_compacted_evidence_do_not_produce_analysis():
    assert onboard({"resourceSpans": []}, format="otlp")["status"] == "no_execution_data"
    source = _quickstart_trace()
    retained = RetentionSession(RetentionPolicy("test", "v1", mode="metrics_only")).retain(source)
    result = onboard(retained.to_dict())["traces"][0]
    assert result["analysis"] is None
    assert "retained_evidence_incomplete" in {
        item["code"] for item in result["validation"]["checks"]
    }
    assert validate_capture(AgentTrace("empty"))["analysis_allowed"] is False


def test_log_only_import_is_not_useful_execution_evidence():
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {"logRecords": [{"traceId": "a" * 32, "body": {"stringValue": "private-body"}}]}
                ]
            }
        ]
    }
    report = onboard(payload, format="otlp")
    assert report["status"] == "no_execution_data"
    assert report["traces"][0]["analysis"] is None
    assert "private-body" not in json.dumps(report)


def test_bounded_analysis_and_payload_free_findings():
    source = _quickstart_trace()
    source.events[0].metadata["private"] = "secret-value"
    source.events[0].output_text = "secret-output"
    result = onboard(source.to_dict())
    assert result["traces"][0]["findings"]
    assert "secret-" not in json.dumps(result)
    assert result["traces"][0]["findings"][0]["affected_nodes"]
    source.events = []
    model = _quickstart_trace().events[0]
    for index in range(2001):
        event = copy.deepcopy(model)
        event.event_id = str(index)
        source.add_event(event)
    result = onboard(source.to_dict())["traces"][0]
    assert result["analysis"] is None
    assert result["analysis_status"] == "unavailable"


def test_environment_enablement_is_explicit_and_does_not_change_business_calls(monkeypatch):
    calls = []
    response = {"usage": {"input_tokens": 1, "output_tokens": 2}}

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setenv("AGENTLOOP_INSTRUMENTATION_ENABLED", "false")
    assert instrument(client, integration="openai") is client
    assert client.responses.create is create
    monkeypatch.setenv("AGENTLOOP_INSTRUMENTATION_ENABLED", "true")
    instrument(client, integration="openai")
    wrapped = client.responses.create
    instrument(client, integration="openai")
    assert client.responses.create is wrapped
    with trace_agent("host-owned") as trace:
        assert client.responses.create(model="test") is response
    assert len(trace.events) == 1
    assert trace.events[0].token_provenance == "provider"
    assert calls == [{"model": "test"}]
    monkeypatch.setenv("AGENTLOOP_INSTRUMENTATION_ENABLED", "invalid")
    with pytest.raises(ValueError):
        instrument(client, integration="openai")


@pytest.mark.parametrize("integration", ["openai", "langgraph_builder", "crewai_task"])
def test_unsupported_targets_do_not_pretend_to_enable_coverage(integration):
    with pytest.raises(TypeError):
        instrument(object(), integration=integration, enabled=True)
    assert instrument(None, integration=integration, enabled=False) is None


def test_onboarding_cli_output_and_input_protection(tmp_path):
    source = _quickstart_trace().export_json(tmp_path / "source.json")
    output = tmp_path / "result.json"
    response = CliRunner().invoke(
        app, ["onboard", str(source), "--out", str(output), "--expected-operation", "model"]
    )
    assert response.exit_code == 0, response.output
    assert "Capture status:" in response.output
    assert json.loads(output.read_text())["trace_count"] == 1
    response = CliRunner().invoke(app, ["onboard", str(source), "--out", str(source)])
    assert response.exit_code != 0
    assert AgentTrace.from_json(source).events
