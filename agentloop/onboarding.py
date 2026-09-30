"""First-use validation of native or standards-imported execution evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from agentloop.metrics import build_report
from agentloop.operations import operation_counts, operation_kind
from agentloop.otel import traces_from_otel
from agentloop.retention import read_retention
from agentloop.schema import coerce_event_dict, validate_trace_dict
from agentloop.timing import event_interval_ms, timestamp_ms
from agentloop.tokens import provenance_grade
from agentloop.tracer import AgentTrace


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_capture(trace: AgentTrace, *, expected_operations: tuple[str, ...] = ()) -> dict:
    """Describe missing evidence without echoing payloads or claiming complete coverage."""
    from agentloop.operations import OPERATION_KINDS

    if not isinstance(trace, AgentTrace):
        raise TypeError("trace must be AgentTrace")
    if not isinstance(expected_operations, tuple) or any(
        kind not in OPERATION_KINDS for kind in expected_operations
    ):
        raise ValueError("expected_operations must be a tuple of supported operation kinds")
    checks = []

    def add(code, severity, count, detail):
        if count:
            checks.append({"code": code, "severity": severity, "count": count, "detail": detail})

    try:
        validate_trace_dict(trace.to_dict())
        for index, event in enumerate(trace.events):
            coerce_event_dict(event.to_dict(), index=index)
        retained = read_retention(trace)
    except (ValueError, TypeError, OverflowError):
        return {
            "schema_version": "1.0",
            "status": "invalid",
            "analysis_allowed": False,
            "checks": [
                {
                    "code": "invalid_native_schema",
                    "severity": "error",
                    "count": 1,
                    "detail": "Trace schema, event fields or retained evidence are invalid.",
                }
            ],
            "coverage": "unverified",
            "operation_counts": {},
        }
    by_id = {event.event_id: event for event in trace.events}
    transport_ids = [
        event.metadata["otel_trace_id"]
        for event in trace.events
        if "otel_trace_id" in event.metadata
    ]
    add(
        "transport_trace_id_missing",
        "error",
        sum(not isinstance(value, str) or not value.strip() for value in transport_ids),
        "Imported spans lack a source trace ID; execution boundaries cannot be established.",
    )
    add(
        "transport_trace_ids_mixed",
        "error",
        int(len({value for value in transport_ids if isinstance(value, str) and value}) > 1),
        "One native trace contains spans with several source trace IDs; keep those executions separate.",
    )
    add(
        "invalid_elapsed_time",
        "error",
        int(
            trace.elapsed_ms is not None
            and (not math.isfinite(trace.elapsed_ms) or trace.elapsed_ms < 0)
        ),
        "Recorded trace elapsed time must be finite and nonnegative.",
    )
    add(
        "no_execution_spans",
        "warning",
        int(not by_id),
        "No execution evidence was captured; evaluation metadata alone does not profile execution.",
    )
    missing = sum(
        event.parent_id is not None and event.parent_id not in by_id for event in trace.events
    )
    add(
        "missing_parent",
        "error",
        missing,
        "A parent is outside this captured trace; export the complete execution boundary.",
    )
    states = {}
    cycles = 0
    for event in trace.events:
        current = event.event_id
        path = []
        while current in by_id and current not in states:
            states[current] = 1
            path.append(current)
            current = by_id[current].parent_id
        if states.get(current) == 1:
            cycles += 1
        for key in path:
            states[key] = 2
    add(
        "parent_cycle",
        "error",
        cycles,
        "Parent relationships contain a cycle and cannot define a valid execution tree.",
    )
    kinds = operation_counts(trace.events)
    add(
        "unsupported_operation",
        "warning",
        kinds.get("unknown", 0),
        "Unknown operation kinds remain unsupported; annotate their actual operation kind.",
    )
    missing_kinds = [kind for kind in expected_operations if not kinds.get(kind)]
    add(
        "expected_operation_missing",
        "warning",
        len(missing_kinds),
        "At least one expected operation kind was not captured; configure its adapter or span hook.",
    )
    models = [event for event in trace.events if operation_kind(event) == "model"]
    incomplete_usage = sum(provenance_grade(event.token_provenance) != "exact" for event in models)
    add(
        "usage_not_exact",
        "warning",
        incomplete_usage,
        "Some model usage is missing, estimated or unspecified; request SDK usage and retain its provenance.",
    )
    missing_models = sum(not event.model for event in models)
    add(
        "model_identity_missing",
        "warning",
        missing_models,
        "Some model calls have no model identity; model pricing and comparisons may be unavailable.",
    )
    start, end = timestamp_ms(trace.started_at), timestamp_ms(trace.ended_at)
    boundary_missing = (
        start is None or end is None or end < start or getattr(trace, "_timing_active", False)
    )
    add(
        "trace_boundary_missing",
        "warning",
        int(boundary_missing),
        "The completed trace boundary is missing or invalid; finish the host execution before analysis.",
    )
    missing_intervals = outside = 0
    for event in trace.events:
        interval = event_interval_ms(event)
        missing_intervals += interval is None
        if interval is not None and not boundary_missing:
            outside += interval[0] < start - 1 or interval[1] > end + 1
    add(
        "span_timing_unavailable",
        "warning",
        missing_intervals,
        "Some span timestamps do not support their recorded durations; timing analysis may use a fallback.",
    )
    add(
        "span_outside_boundary",
        "warning",
        outside,
        "Some spans extend outside the host trace boundary; finish streams and callbacks before finalizing the run.",
    )
    text_fields = sum(
        getattr(event, key) is not None
        for event in trace.events
        for key in ("input_text", "output_text", "error")
    )
    metadata_events = sum(bool(event.metadata) for event in trace.events)
    add(
        "payload_capture_present",
        "warning",
        text_fields,
        "Captured input/output/error text can contain private data; apply an explicit retention policy before sharing.",
    )
    add(
        "metadata_requires_review",
        "notice",
        metadata_events + bool(trace.metadata),
        "Metadata and labels can contain private data, including imported attributes/logs; privacy has not been certified.",
    )
    add(
        "retained_evidence_incomplete",
        "warning",
        int(retained is not None and not retained["complete_evidence"]),
        "Retention omitted analysis evidence; use the original trace for findings or replay.",
    )
    roots = sum(event.parent_id is None for event in trace.events)
    add(
        "multiple_roots",
        "notice",
        int(roots > 1),
        "Several root spans are present; confirm that they belong to one host execution rather than unrelated tasks.",
    )
    invalid = any(item["severity"] == "error" for item in checks)
    partial = any(item["severity"] == "warning" for item in checks)
    return {
        "schema_version": "1.0",
        "status": "invalid" if invalid else "partial" if partial else "ready",
        "analysis_allowed": not invalid
        and bool(by_id)
        and not (retained and not retained["complete_evidence"]),
        "checks": checks,
        "operation_counts": kinds,
        "expected_operations": list(expected_operations),
        "missing_expected_operations": missing_kinds,
        "event_count": len(by_id),
        "root_count": roots,
        "usage_provenance_grades": dict(
            Counter(provenance_grade(event.token_provenance) for event in models)
        ),
        "privacy": {
            "captured_text_field_count": text_fields,
            "metadata_event_count": metadata_events,
            "certified_safe": False,
        },
        "coverage": "Only supplied spans are observable; absence of warnings does not prove complete instrumentation.",
    }


def onboard(
    payload, *, format="native", expected_operations: tuple[str, ...] = (), analyze=True
) -> dict:
    """Import without business-logic changes, validate, then produce a payload-free summary."""
    if format not in {"native", "otlp"}:
        raise ValueError(
            "format must be native or otlp (including supported OpenInference attributes)"
        )
    if type(analyze) is not bool:
        raise TypeError("analyze must be boolean")
    traces = [AgentTrace.from_dict(payload)] if format == "native" else traces_from_otel(payload)
    results = []
    for trace in traces:
        validation = validate_capture(trace, expected_operations=expected_operations)
        report = None
        findings = []
        if analyze and validation["analysis_allowed"] and len(trace.events) <= 2000:
            original = build_report(trace)
            report = {
                key: original[key]
                for key in (
                    "event_count",
                    "operation_counts",
                    "total_runtime_ms",
                    "model_call_count",
                    "tool_call_count",
                    "input_tokens",
                    "output_tokens",
                    "token_status",
                    "estimated_cost_usd",
                    "cost_status",
                    "analysis_complete",
                )
            }
            findings = [
                {
                    key: item.get(key)
                    for key in (
                        "rule_id",
                        "rule_version",
                        "type",
                        "confidence",
                        "affected_nodes",
                        "evidence_level",
                        "estimated_latency_savings_ms",
                        "estimated_cost_savings_usd",
                    )
                }
                for item in original["finding_candidates"]
            ]
        results.append(
            {
                "run_id": trace.run_id,
                "source_sha256": _hash(trace.to_dict()),
                "validation": validation,
                "analysis": report,
                "findings": findings,
                "analysis_status": "performed"
                if report is not None
                else "not_requested"
                if not analyze
                else "unavailable",
                "analysis_limit_events": 2000,
            }
        )
    return {
        "schema_version": "1.0",
        "format": format,
        "source_sha256": _hash(payload),
        "trace_count": len(traces),
        "traces": results,
        "status": "no_execution_data"
        if not traces or all(not trace.events for trace in traces)
        else "invalid"
        if any(item["validation"]["status"] == "invalid" for item in results)
        else "partial"
        if any(item["validation"]["status"] == "partial" for item in results)
        else "ready",
        "privacy": "Raw input/output/error text and arbitrary metadata are excluded. Run/span IDs must be opaque host identifiers.",
        "interpretation": "Findings are estimates requiring validation; supplied telemetry is not a coverage or task-quality guarantee.",
    }
