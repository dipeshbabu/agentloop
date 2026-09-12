from __future__ import annotations

from copy import deepcopy

import pytest

from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis
from agentloop.graph import ExecutionGraph
from agentloop.operations import OPERATION_KINDS
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace, trace_agent, trace_tool_call


@pytest.mark.parametrize("kind", sorted(OPERATION_KINDS))
def test_native_operation_kind_survives_storage_graph_and_repeated_otlp_roundtrips(kind):
    trace = _quickstart_trace()
    trace.events[0].metadata["operation_kind"] = kind
    loaded = AgentTrace.from_dict(trace.to_dict())
    assert loaded.events[0].operation_kind == kind
    graph = ExecutionGraph.from_trace(loaded)
    node = next(node for node in graph.nodes if node.node_id == loaded.events[0].event_id)
    assert node.to_dict()["operation_kind"] == kind
    for _ in range(2):
        loaded = trace_from_otel(trace_to_otel(loaded))
        assert loaded.events[0].operation_kind == kind
        assert loaded.events[0].event_type == trace.events[0].event_type
        assert loaded.events[0].metadata["operation_kind"] == kind
        assert loaded.events[0].parent_id == trace.events[0].parent_id
    replay = build_replay_report(trace, loaded)
    assert replay["baseline"]["operation_counts"] == replay["candidate"]["operation_counts"]


def test_legacy_1_0_traces_keep_counts_metrics_and_findings():
    original = _quickstart_trace()
    payload = original.to_dict()
    payload["schema_version"] = "1.0"
    for event in payload["events"]:
        event.pop("token_provenance", None)
    legacy = AgentTrace.from_dict(payload)
    for field in (
        "event_count",
        "model_call_count",
        "tool_call_count",
        "retry_count",
        "total_runtime_ms",
    ):
        assert legacy.report()[field] == original.report()[field]
    assert [finding["finding_id"] for finding in build_diagnosis(legacy)["findings"]] == [
        finding["finding_id"] for finding in build_diagnosis(original)["findings"]
    ]
    assert all(
        event.operation_kind
        == {"tool_call": "tool", "model_call": "model", "retry": "retry"}[event.event_type]
        for event in legacy.events
    )


@pytest.mark.parametrize("raw", ["future_step", "  FUTURE_KIND  "])
def test_unknown_native_labels_are_preserved_but_never_treated_as_tools(raw):
    trace = _quickstart_trace()
    for event in trace.events:
        event.metadata["operation_kind"] = raw
    loaded = trace_from_otel(trace_to_otel(AgentTrace.from_dict(trace.to_dict())))
    assert all(event.operation_kind == "unknown" for event in loaded.events)
    assert all(event.metadata["operation_kind"] == raw for event in loaded.events)
    assert loaded.report()["parallelism_opportunities"] == []
    assert loaded.report()["operation_counts"] == {"unknown": len(trace.events)}
    assert not any(
        card["type"]
        in {
            "cache_context",
            "batch_model_calls",
            "route_to_smaller_model",
            "split_large_step",
            "add_schema_validation",
        }
        for card in loaded.report()["finding_candidates"]
    )


def test_integration_can_supply_kind_without_framework_dependencies():
    with trace_agent("workflow") as trace:
        with trace_tool_call("fetch", metadata={"operation_kind": " RETRIEVER "}):
            pass
    assert trace.events[0].operation_kind == "retriever"
    assert trace.events[0].event_type == "tool_call"


@pytest.mark.parametrize(
    "operation,kind",
    [
        ("invoke_agent", "agent"),
        ("invoke_workflow", "workflow"),
        ("retrieval", "retriever"),
        ("execute_tool", "tool"),
        ("chat", "model"),
        ("custom_future_operation", "unknown"),
    ],
)
def test_third_party_otlp_operations_are_distinct(operation, kind):
    payload = {
        "spans": [
            {
                "traceId": "a" * 32,
                "spanId": "b" * 16,
                "name": "tool agent workflow",
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "1100000000",
                "attributes": {"gen_ai.operation.name": operation},
            }
        ]
    }
    trace = trace_from_otel(payload)
    assert trace.events[0].operation_kind == kind
    assert trace.report()["operation_counts"] == {kind: 1}
    if kind == "unknown":
        assert trace.events[0].metadata["operation_kind"] == operation
    assert trace_from_otel(trace_to_otel(trace)).events[0].operation_kind == kind


def test_agent_and_workflow_spans_are_excluded_from_tool_savings():
    trace = _quickstart_trace()
    for event in trace.events:
        if event.event_type == "tool_call":
            event.metadata["operation_kind"] = "workflow"
    assert trace.report()["tool_call_count"] > 0  # legacy category is retained
    assert trace.report()["parallelism_opportunities"] == []
    assert not any(
        finding["type"] in {"parallelize_tools", "tool_oscillation"}
        for finding in build_diagnosis(trace)["findings"]
    )


def test_findings_carry_operation_kind_with_span_evidence():
    diagnosis = build_diagnosis(_quickstart_trace())
    assert diagnosis["findings"]
    for finding in diagnosis["findings"]:
        assert all(row["operation_kind"] in OPERATION_KINDS for row in finding["evidence"])


def test_roundtrip_does_not_add_operation_metadata_to_legacy_native_events():
    trace = _quickstart_trace()
    expected = [deepcopy(event.metadata) for event in trace.events]
    loaded = trace_from_otel(trace_to_otel(trace))
    for event, metadata in zip(loaded.events, expected):
        assert {
            key: value
            for key, value in event.metadata.items()
            if key not in {"otel_span_id", "otel_trace_id"}
        } == metadata
