from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agentloop.entrypoint import _quickstart_trace
from agentloop.graph import ExecutionGraph
from agentloop.otel import trace_from_otel, trace_to_otel, traces_from_otel
from agentloop.schema import TraceValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "telemetry"


def fixture(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert data["fixture_schema_version"] == "1.0"
    assert len(data["upstream"]["revision"]) == 40
    assert data["upstream"]["revision"] in data["upstream"]["source"]
    assert data["synthetic"] is True
    return data["payload"]


def test_genai_usage_evaluations_links_and_trace_boundaries():
    traces = traces_from_otel(fixture("genai"))
    assert len(traces) == 2
    trace = traces[0]
    assert [event.operation_kind for event in trace.events] == [
        "workflow",
        "agent",
        "model",
        "tool",
        "memory",
    ]
    model = trace.events[2]
    assert model.model == "gpt-4o"  # billed response model takes precedence
    assert (model.input_tokens, model.output_tokens, model.token_provenance) == (
        100,
        20,
        "provider",
    )
    assert model.metadata["cached_input_tokens"] == 40
    assert model.metadata["cache_write_input_tokens"] == 5
    assert model.metadata["reasoning_output_tokens"] == 8
    assert model.metadata["gen_ai.usage.future_modality_tokens"] == 3
    assert model.metadata["vendor.structured"] == {"items": [1, True, "synthetic"]}
    assert model.metadata["otel_links"][0]["traceId"] == "b" * 32
    assert model.metadata["otel_links"][0]["spanId"] == "0000000000000006"
    assert model.metadata["otel_span_flags"] == 1
    assert model.metadata["evaluation_results"][0]["score"] == 4.0
    assert model.metadata["evaluation_results"][1]["name"] == "correctness"
    assert (
        model.metadata["otel_log_records"][0]["record"]["eventName"] == "gen_ai.evaluation.result"
    )
    assert "quality_score" not in trace.metadata
    assert "quality_score" not in trace.report()
    assert trace.events[1].metadata["conversation_id"] == "conversation-1"
    assert trace.events[3].metadata["tool_call_id"] == "call-1"


def test_openinference_kind_and_usage_mapping():
    trace = trace_from_otel(fixture("openinference"))
    assert [event.operation_kind for event in trace.events] == [
        "workflow",
        "agent",
        "model",
        "tool",
        "evaluator",
        "guardrail",
        "retriever",
        "reranker",
    ]
    assert trace.events[0].metadata["session_id"] == "session-1"
    model = trace.events[2]
    assert (model.input_tokens, model.output_tokens, model.token_provenance) == (
        200,
        30,
        "provider",
    )
    assert model.metadata["cached_input_tokens"] == 50
    assert model.metadata["reasoning_output_tokens"] == 10
    assert model.metadata["provider_reported_cost_usd"] == 0.004
    assert trace.report()["estimated_cost_usd"] == 0.004
    assert trace.events[3].metadata["tool_call_id"] == "call-oi"
    assert trace.events[4].metadata["evaluation_results"][0]["name"] == "relevance"


def test_mcp_preserves_client_transport_server_tool_relationship_and_errors():
    trace = trace_from_otel(fixture("mcp"))
    assert [event.operation_kind for event in trace.events] == ["tool", "unknown", "tool", "tool"]
    assert [event.metadata["otel_span_kind"] for event in trace.events] == [3, 3, 2, 1]
    assert [event.parent_id for event in trace.events] == [
        None,
        *[event.event_id for event in trace.events[:-1]],
    ]
    assert trace.events[0].metadata["session_id"] == "mcp-session-1"
    assert trace.events[0].metadata["mcp.protocol.version"] == "2025-06-18"
    assert trace.events[0].metadata["jsonrpc.request.id"] == "request-1"
    assert trace.events[3].status == "error"
    assert trace.events[3].error == "synthetic failure"
    graph = ExecutionGraph.from_trace(trace)
    assert {(edge.source, edge.target) for edge in graph.edges if edge.kind == "parent"} == {
        (trace.events[index].event_id, trace.events[index + 1].event_id) for index in range(3)
    }
    assert trace.report()["model_call_count"] == 0


@pytest.mark.parametrize("name", ["genai", "openinference", "mcp"])
def test_imported_convention_data_remains_stable_over_repeated_native_roundtrips(name):
    for original in traces_from_otel(fixture(name)):
        trace = original
        for _ in range(2):
            trace = trace_from_otel(trace_to_otel(trace))
            assert trace.run_id == original.run_id
            assert trace.metadata == original.metadata
            assert trace.report()["total_runtime_ms"] == original.report()["total_runtime_ms"]
            for before, after in zip(original.events, trace.events):
                assert (
                    after.event_id,
                    after.parent_id,
                    after.operation_kind,
                    after.model,
                    after.status,
                    after.error,
                ) == (
                    before.event_id,
                    before.parent_id,
                    before.operation_kind,
                    before.model,
                    before.status,
                    before.error,
                )
                assert after.metadata == before.metadata
                assert (after.input_tokens, after.output_tokens) == (
                    before.input_tokens,
                    before.output_tokens,
                )


def test_native_complex_metadata_and_legacy_usage_are_not_upgraded_or_lost():
    original = _quickstart_trace()
    original.metadata = {"experiment": {"task": 1, "seeds": [1, 2], "optional": None}}
    original.elapsed_ms = 12345.0
    event = original.events[0]
    event.token_provenance = None
    event.metadata["nested"] = {"false": False, "array": [1, None, {"value": 2.5}]}
    event.duration_ms = 123.456789
    imported = trace_from_otel(trace_to_otel(original))
    assert imported.metadata == original.metadata
    assert imported.elapsed_ms == 12345.0
    assert imported.events[0].duration_ms == 123.456789
    assert imported.events[0].token_provenance is None
    assert imported.events[0].metadata["nested"] == event.metadata["nested"]


def test_explicit_zero_usage_wins_over_legacy_aliases():
    payload = {
        "spans": [
            {
                "traceId": "e" * 32,
                "spanId": "1" * 16,
                "attributes": {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.usage.input_tokens": 0,
                    "llm.token_count.prompt": 999,
                },
            }
        ]
    }
    assert trace_from_otel(payload).events[0].input_tokens == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "invalid"])
def test_invalid_usage_is_rejected_instead_of_truncated(value):
    payload = {
        "spans": [
            {
                "traceId": "e" * 32,
                "spanId": "1" * 16,
                "attributes": {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": value},
            }
        ]
    }
    with pytest.raises(TraceValidationError):
        trace_from_otel(payload)


def test_source_fixture_objects_are_not_mutated_during_import():
    payload = fixture("genai")
    before = deepcopy(payload)
    traces_from_otel(payload)
    assert payload == before


def test_evaluation_only_trace_contains_no_invented_execution_spans():
    payload = {"resourceLogs": fixture("genai")["resourceLogs"]}
    trace = trace_from_otel(payload)
    assert trace.events == []
    assert trace.metadata["execution_data_present"] is False
    assert trace.metadata["evaluation_results"][0]["score"] == 0.9
    assert trace.report()["event_count"] == 0
    assert trace.report()["total_runtime_ms"] == 0
    imported = trace_from_otel(trace_to_otel(trace))
    assert imported.metadata == trace.metadata
    assert imported.run_id == trace.run_id


def test_logs_are_associated_by_trace_and_span_together():
    payload = fixture("genai")
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["traceId"] = "b" * 32
    traces = traces_from_otel(payload)
    assert "otel_log_records" not in traces[0].events[2].metadata
    assert traces[1].metadata["otel_log_records"][0]["record"]["spanId"] == record["spanId"]


@pytest.mark.parametrize("kind", ["PROMPT", "FUTURE_KIND"])
def test_unsupported_openinference_kinds_are_preserved_without_model_semantics(kind):
    trace = trace_from_otel(
        {
            "spans": [
                {
                    "traceId": "f" * 32,
                    "spanId": "1" * 16,
                    "attributes": {"openinference.span.kind": kind},
                }
            ]
        }
    )
    assert trace.events[0].operation_kind == "unknown"
    assert trace.events[0].metadata["openinference.span.kind"] == kind
    assert trace.report()["model_call_count"] == 0
