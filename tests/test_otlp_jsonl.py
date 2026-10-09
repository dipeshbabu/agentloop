"""Bounded multi-document telemetry, source qualification and converter parity."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, app
from agentloop.html_report import analysis_to_html
from agentloop.integrations.harbor.atif import import_atif
from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp
from agentloop.interoperability.validation import ImportLimits, ImportValidationError
from agentloop.otel import traces_from_otel
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures/external/otlp"
TID = "a" * 32


def span(sid="1" * 16, trace=TID, *, attrs=None, start="1000000000", end="2000000000", **extra):
    return {
        "traceId": trace,
        "spanId": sid,
        "name": "chat",
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "status": {"code": 1},
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "fixture-model",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 6,
            **(attrs or {}),
        },
        **extra,
    }


def document(*spans, resource=None, scope=None):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource or {"service.name": "fixture"}},
                "scopeSpans": [
                    {"scope": scope or {"name": "fixture", "version": "1"}, "spans": list(spans)}
                ],
            }
        ]
    }


def source(tmp_path, documents, *, name="export.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(value) + "\n" for value in documents), encoding="utf-8")
    return path


def test_frozen_multi_trace_and_duplicate_record_reconciliation():
    first = import_otlp(FIXTURES / "two_records.jsonl")
    assert len(first.traces) == 2
    assert first.inventory()["physical_records"] == 2
    duplicate = import_otlp(FIXTURES / "duplicate_trace_ids.jsonl")
    assert len(duplicate.traces) == 1
    assert len(duplicate.traces[0].events) == 2
    assert duplicate.inventory()["raw_spans"] == 3
    assert duplicate.inventory()["identical_duplicate_spans"] == 1
    root, child = duplicate.traces[0].events
    assert child.parent_id == root.event_id


def test_one_line_multiple_traces_is_not_collapsed(tmp_path):
    result = import_otlp(source(tmp_path, [document(span(), span(trace="b" * 32))]))
    assert len(result.traces) == 2
    assert result.inventory()["valid_records"] == 1
    assert {item.to_dict()["external_identity"]["trace_id"] for item in result.source_receipts} == {
        TID,
        "b" * 32,
    }


def test_late_parent_is_resolved_only_within_its_trace(tmp_path):
    child = span("2" * 16, parentSpanId="1" * 16)
    result = import_otlp(source(tmp_path, [document(child), document(span())]))
    by_span = {event.metadata["otel_span_id"]: event for event in result.traces[0].events}
    assert by_span["2" * 16].parent_id == by_span["1" * 16].event_id
    other = import_otlp(
        source(tmp_path, [document(child), document(span(trace="b" * 32))], name="cross.jsonl")
    )
    assert other.traces[0].events[0].parent_id is None
    assert other.traces[0].events[0].metadata["unresolved_parent_span_id"] == "1" * 16


def test_conflicting_span_segment_never_overwrites_first_source(tmp_path):
    first, changed = span(), span(end="9000000000", attrs={"gen_ai.usage.input_tokens": 999})
    result = import_otlp(source(tmp_path, [document(first), document(changed)]))
    assert result.inventory()["conflicting_span_segments"] == 1
    assert result.inventory()["identical_duplicate_spans"] == 0
    trace = result.traces[0]
    assert len(trace.events) == 1 and trace.events[0].input_tokens == 12
    assert trace.report()["total_runtime_ms"] is None
    assert result.source_receipts[0].to_dict()["source_metadata"]["conflicting_segments"] == 1
    with pytest.raises(ValueError, match="complete external trial"):
        build_replay_report(trace, trace)


def test_same_identity_different_omitted_payload_is_still_a_conflict(tmp_path):
    first = span(attrs={"input.value": "private-first"})
    second = span(attrs={"input.value": "private-second"})
    result = import_otlp(source(tmp_path, [document(first), document(second)]))
    assert result.inventory()["conflicting_span_segments"] == 1
    exported = json.dumps(result.traces[0].to_dict())
    assert "private-first" not in exported and "private-second" not in exported


def test_malformed_record_keeps_valid_later_record_and_payload_free_notice():
    result = import_otlp(FIXTURES / "malformed_mixed_valid.jsonl")
    assert len(result.traces) == 1
    assert result.inventory()["invalid_records"] == 1
    assert result.inventory()["valid_records"] == 1
    assert result.inventory()["record_rows"][0]["record"] == 1
    assert result.source_receipts[-1].to_dict()["missing_trace_reason"] == "invalid_artifact"


def test_non_utf8_blank_lines_and_final_line_without_newline(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"\n\r\n\xff\n" + json.dumps(document(span())).encode())
    result = import_otlp(path)
    assert result.inventory()["physical_records"] == 4
    assert result.inventory()["blank_lines"] == 2
    assert result.inventory()["invalid_records"] == 1
    assert len(result.traces) == 1
    assert result.inventory()["artifact_sha256"] == sha256(path.read_bytes()).hexdigest()


def test_oversized_line_is_drained_in_bounded_chunks_and_later_record_survives(tmp_path):
    path = tmp_path / "input.jsonl"
    valid = json.dumps(document(span())).encode() + b"\n"
    path.write_bytes(b"x" * 5000 + b"\n" + valid)
    result = import_otlp(path, limits=replace(ImportLimits(), max_line_bytes=1024))
    assert result.inventory()["invalid_records"] == 1
    assert result.inventory()["physical_records"] == 2
    assert len(result.traces) == 1
    assert result.inventory()["artifact_sha256"] == sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "limit", ["max_total_bytes", "max_records", "max_spans", "max_trajectories"]
)
def test_overall_limits_are_terminal_even_in_partial_mode(tmp_path, limit):
    path = source(tmp_path, [document(span()), document(span(trace="b" * 32))])
    with pytest.raises(ImportValidationError, match="budget"):
        import_otlp(path, limits=replace(ImportLimits(), **{limit: 1}))


def test_merged_event_limit_is_not_reset_for_each_line(tmp_path):
    path = source(tmp_path, [document(span()), document(span("2" * 16))])
    with pytest.raises(ImportValidationError, match="merged trace"):
        import_otlp(path, limits=replace(ImportLimits(), max_events_per_trace=1))


def test_strict_mode_rejects_independent_invalid_record():
    with pytest.raises(ImportValidationError):
        import_otlp(
            FIXTURES / "malformed_mixed_valid.jsonl", options=OtlpOptions(continue_on_error=False)
        )


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
        {"resourceSpans": {}},
        document({"traceId": 123}),
        document(span(attrs={"gen_ai.usage.input_tokens": True})),
        document(span(status={"code": True})),
        document(span(links=["bad"])),
        document(span(events=["bad"])),
    ],
)
def test_invalid_structures_are_diagnostics_without_unhandled_errors(tmp_path, bad):
    result = import_otlp(source(tmp_path, [bad, document(span(trace="b" * 32))]))
    assert result.inventory()["invalid_records"] == 1
    assert len(result.traces) == 1


def test_duplicate_attribute_names_and_invalid_anyvalue_are_rejected(tmp_path):
    for attrs in (
        [
            {"key": "operation", "value": {"stringValue": "a"}},
            {"key": "operation", "value": {"stringValue": "b"}},
        ],
        [{"key": "kind", "value": {"stringValue": "a", "boolValue": True}}],
    ):
        value = span()
        value["attributes"] = attrs
        result = import_otlp(source(tmp_path, [document(value)]))
        assert result.inventory()["invalid_records"] == 1
        assert not result.traces


@pytest.mark.parametrize("representation", ["wire", "usage", "timestamp"])
def test_numeric_decimal_strings_are_bounded_before_conversion(tmp_path, representation):
    value = span()
    if representation == "wire":
        value["attributes"] = [
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "9" * 500}}
        ]
    elif representation == "usage":
        value["attributes"]["gen_ai.usage.input_tokens"] = "9" * 500
    else:
        value["startTimeUnixNano"] = "9" * 500
    result = import_otlp(source(tmp_path, [document(value), document(span(trace="b" * 32))]))
    assert result.inventory()["invalid_records"] == 1
    assert len(result.traces) == 1


def test_missing_identifiers_have_calculated_isolated_identity_not_shared_fake_source(tmp_path):
    missing = span()
    missing.pop("traceId")
    missing.pop("spanId")
    result = import_otlp(source(tmp_path, [document(missing), document(missing)]))
    assert len(result.traces) == 2
    assert len({trace.run_id for trace in result.traces}) == 2
    assert result.inventory()["identical_duplicate_spans"] == 0
    for receipt in result.source_receipts:
        value = receipt.to_dict()
        assert value["external_identity"]["trace_id"] is None
        assert value["identity_provenance"]["trajectory_id"] == "calculated"
    assert all(trace.report()["total_runtime_ms"] is None for trace in result.traces)


def test_span_links_are_retained_as_relationships_without_parent_edges(tmp_path):
    value = span(
        links=[{"traceId": "b" * 32, "spanId": "2" * 16, "attributes": {"link.type": "handoff"}}]
    )
    result = import_otlp(source(tmp_path, [document(value)]))
    event = result.traces[0].events[0]
    assert event.parent_id is None
    assert event.metadata["otel_links"][0]["traceId"] == "b" * 32


def test_snake_case_aliases_preserve_source_identity_and_reject_conflicts(tmp_path):
    value = span()
    for canonical, alias in (
        ("traceId", "trace_id"),
        ("spanId", "span_id"),
        ("startTimeUnixNano", "start_time_unix_nano"),
        ("endTimeUnixNano", "end_time_unix_nano"),
    ):
        value[alias] = value.pop(canonical)
    result = import_otlp(source(tmp_path, [document(value)]))
    assert result.source_receipts[0].to_dict()["external_identity"]["trace_id"] == TID
    assert result.traces[0].report()["total_runtime_ms"] == 1000
    value["traceId"] = "b" * 32
    assert import_otlp(source(tmp_path, [document(value)])).inventory()["invalid_records"] == 1


def test_unrepresented_structural_fields_have_source_qualified_drop_notice(tmp_path):
    payload = document(span(unsupported={"private": "PRIVATE_UNKNOWN"}))
    payload["extra"] = {"hidden": 4}
    result = import_otlp(source(tmp_path, [payload]))
    assert result.inventory()["notices_by_code"]["unrepresented_structural_fields"] == 1
    assert result.traces[0].events[0].metadata["unrepresented_structural_fields"] == 2
    assert "PRIVATE_UNKNOWN" not in json.dumps(result.traces[0].to_dict())


def test_missing_timing_status_and_usage_do_not_become_zero_success(tmp_path):
    value = span(attrs={})
    value.pop("endTimeUnixNano")
    value.pop("status")
    value["attributes"].pop("gen_ai.usage.output_tokens")
    trace = import_otlp(source(tmp_path, [document(value)])).traces[0]
    analysis = _analysis_payload(trace)
    report = analysis["report"]
    assert report["total_runtime_ms"] is None
    assert report["output_tokens"] is None
    assert report["estimated_cost_usd"] is None
    assert report["events"][0]["status"] == "unknown"
    assert report["events"][0]["duration_ms"] is None
    assert "unavailable" in analysis_to_html(analysis)


def test_single_document_parity_preserves_native_semantics_with_source_qualification(tmp_path):
    payload = document(
        span(
            attrs={
                "gen_ai.provider.name": "fixture-provider",
                "gen_ai.usage.cache_read.input_tokens": 4,
                "custom.number": 8,
                "session.id": "session-1",
            }
        )
    )
    path = tmp_path / "single.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    batch = import_otlp(path)
    old = traces_from_otel(payload)[0]
    event = batch.traces[0].events[0]
    for key in (
        "event_type",
        "operation_kind",
        "model",
        "input_tokens",
        "output_tokens",
        "duration_ms",
    ):
        assert getattr(event, key) == getattr(old.events[0], key)
    assert event.metadata["cached_input_tokens"] == 4
    assert event.metadata["custom.number"] == 8
    assert batch.source_receipts[0].to_dict()["external_identity"]["session_id"] == "session-1"
    assert event.token_provenance == "external_reported"
    assert batch.traces[0].report()["estimated_cost_usd"] is None


def test_native_id_assertions_are_retained_without_overriding_wire_parentage(tmp_path):
    value = span(
        attrs={
            "agentloop.native_event_id": "original-event",
            "agentloop.native_parent_id": "span_" + "2" * 16,
        }
    )
    result = import_otlp(
        source(
            tmp_path,
            [document(value, span("2" * 16), resource={"agentloop.run_id": "original-run"})],
        )
    )
    event = result.traces[0].events[0]
    assert result.traces[0].run_id != "original-run"
    assert event.parent_id is None
    assert event.metadata["source_native_identity"] == {
        "run_id": "original-run",
        "event_id": "original-event",
        "parent_id": "span_" + "2" * 16,
    }


def test_calculated_missing_span_identity_cannot_resolve_a_source_parent(tmp_path):
    missing = span()
    missing.pop("spanId")
    path = source(tmp_path, [document(missing)])
    calculated = import_otlp(path).traces[0].events[0].metadata["otel_span_id"]
    child = span("2" * 16, parentSpanId=calculated)
    result = import_otlp(source(tmp_path, [document(missing), document(child)]))
    assert result.traces[0].events[1].parent_id is None
    assert result.inventory()["notices_by_code"]["missing_span_identity"] == 1


def test_nested_payload_cannot_leak_through_native_identity_assertion(tmp_path):
    invalid = document(span(), resource={"agentloop.run_id": {"access_token": "PRIVATE_NESTED"}})
    result = import_otlp(source(tmp_path, [invalid, document(span(trace="b" * 32))]))
    assert result.inventory()["invalid_records"] == 1
    assert "PRIVATE_NESTED" not in json.dumps(result.inventory())
    assert "PRIVATE_NESTED" not in json.dumps(
        [receipt.to_dict() for receipt in result.source_receipts]
    )


def test_default_minimization_covers_inputs_reasoning_credentials_and_errors(tmp_path):
    value = span(
        attrs={
            "input.value": "PRIVATE_INPUT",
            "output.value": "PRIVATE_OUTPUT",
            "metadata.reasoning_content": "PRIVATE_REASONING",
            "access_token": "PRIVATE_CREDENTIAL",
            "exception.message": "PRIVATE_EXCEPTION",
        },
        status={"code": 2, "message": "PRIVATE_STATUS"},
        events=[{"name": "exception", "attributes": {"exception.stacktrace": "PRIVATE_STACK"}}],
    )
    result = import_otlp(source(tmp_path, [document(value)]))
    content = json.dumps(result.traces[0].to_dict()) + json.dumps(result.inventory())
    assert "PRIVATE_" not in content
    opt_in = import_otlp(
        source(tmp_path, [document(value)]), options=OtlpOptions(capture_content=True)
    )
    captured = json.dumps(opt_in.traces[0].to_dict())
    assert "PRIVATE_INPUT" in captured
    assert "PRIVATE_CREDENTIAL" not in captured and "PRIVATE_REASONING" not in captured


def test_source_aggregates_and_cached_subset_are_not_added_to_model_usage(tmp_path):
    root = span(
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 999,
            "llm.cost.total": 1,
        }
    )
    model = span(
        "2" * 16,
        parentSpanId="1" * 16,
        attrs={"gen_ai.usage.cache_read.input_tokens": 4, "llm.cost.total": 0.1},
    )
    result = import_otlp(source(tmp_path, [document(root, model)]))
    report = result.traces[0].report()
    assert report["input_tokens"] == 12 and report["output_tokens"] == 6
    assert report["estimated_cost_usd"] is None
    assert (
        result.source_receipts[0].to_dict()["source_metadata"]["source_aggregates"][0][
            "input_tokens"
        ]
        == 1000
    )


def test_actual_pinned_converter_fixtures_match_source_bytes_and_do_not_fake_latency():
    matrix = json.loads((FIXTURES / "converter_provenance.json").read_text(encoding="utf-8"))
    assert matrix["upstream_revision"] == "d5ac1be17f575852eaf4fffc4072fd18481c209b"
    assert matrix["opentelemetry_proto"] == "1.42.1"
    for case in matrix["cases"]:
        input_path = FIXTURES.parent / case["input"]
        output_path = FIXTURES.parent / case["output"]
        assert sha256(input_path.read_bytes()).hexdigest() == case["input_sha256"]
        assert sha256(output_path.read_bytes()).hexdigest() == case["output_sha256"]
        result = import_otlp(output_path, options=OtlpOptions(system="harbor"))
        assert result.traces
        for trace in result.traces:
            assert trace.report()["total_runtime_ms"] is None
            assert trace.report()["estimated_cost_usd"] is None
            assert all(event.metadata["timing_provenance"] == "inferred" for event in trace.events)
            assert all(not event.metadata["source_status_available"] for event in trace.events)
            assert "SYNTHETIC_REASONING" not in json.dumps(trace.to_dict())
        assert result.inventory()["notices_by_code"]["converter_inferred_timing_status"] > 0
    multimodal = (FIXTURES / "converter_atif_v18_multimodal.jsonl").read_text(encoding="utf-8")
    assert "[audio:" in multimodal and "[image:" in multimodal
    direct = import_atif(FIXTURES.parent / "harbor/atif_v17_simple.json").traces[0]
    converted = import_otlp(
        FIXTURES / "converter_atif_v17_simple.jsonl", options=OtlpOptions(system="harbor")
    ).traces[0]
    assert direct.report()["input_tokens"] == converted.report()["input_tokens"] == 12
    assert direct.report()["output_tokens"] == converted.report()["output_tokens"] == 6


def test_aggregated_converter_block_keeps_multiplicity_separate_from_timed_calls():
    result = import_otlp(FIXTURES / "converter_atif_aggregated_llm_calls.jsonl")
    report = result.traces[0].report()
    assert report["model_call_count"] == 1
    assert report["reported_model_call_count"] == 3
    assert report["model_time_ms"] is None


def test_empty_and_log_only_documents_have_no_manufactured_success_trace(tmp_path):
    empty = import_otlp(source(tmp_path, [document()]))
    assert not empty.traces
    assert empty.source_receipts[0].to_dict()["missing_trace_reason"] == "no_execution_spans"
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "traceId": TID,
                                "spanId": "1" * 16,
                                "attributes": {
                                    "gen_ai.evaluation.name": "correctness",
                                    "gen_ai.evaluation.score.value": 0.5,
                                },
                                "body": {"stringValue": "PRIVATE_BODY"},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    result = import_otlp(source(tmp_path, [payload]))
    assert not result.traces
    receipt = result.source_receipts[0].to_dict()
    assert receipt["outcome"]["quality_pass"] is None
    assert receipt["source_metadata"]["source_logs"]
    assert "PRIVATE_BODY" not in json.dumps(receipt)


def test_native_round_trip_stable_export_and_mutation_guard(tmp_path):
    path = source(tmp_path, [document(span())])
    first, second = import_otlp(path), import_otlp(path)
    assert first.inventory() == second.inventory()
    trace = first.traces[0]
    assert AgentTrace.from_dict(trace.to_dict()).to_dict() == trace.to_dict()
    first.write(tmp_path / "out")
    second.write(tmp_path / "out")
    trace.events[0].input_tokens = 123
    with pytest.raises(ImportValidationError, match="changed after receipt"):
        first.write(tmp_path / "out")


def test_unknown_operation_is_not_manufactured_model_inference(tmp_path):
    value = span()
    value["attributes"] = {}
    event = import_otlp(source(tmp_path, [document(value)])).traces[0].events[0]
    assert event.operation_kind == "unknown"
    assert event.event_type == "source_span"


def test_unsupported_operation_and_recorded_resource_session_are_preserved(tmp_path):
    value = span(attrs={"gen_ai.operation.name": "vendor_future_operation"})
    result = import_otlp(
        source(tmp_path, [document(value, resource={"session.id": "resource-session"})])
    )
    assert result.traces[0].events[0].event_type == "source_span"
    assert (
        result.source_receipts[0].to_dict()["external_identity"]["session_id"] == "resource-session"
    )


def test_parent_cycles_stay_visible_without_breaking_native_analysis(tmp_path):
    result = import_otlp(
        source(
            tmp_path,
            [
                document(span(parentSpanId="2" * 16)),
                document(span("2" * 16, parentSpanId="1" * 16)),
            ],
        )
    )
    trace = result.traces[0]
    assert all(event.parent_id is None for event in trace.events)
    assert result.inventory()["notices_by_code"]["cyclic_parentage"] == 2
    assert _analysis_payload(trace)["report"]["total_runtime_ms"] is None


def test_late_evaluation_logs_survive_duplicate_span_segments(tmp_path):
    first = document(span())
    later = deepcopy(first)
    later["resourceLogs"] = [
        {
            "scopeLogs": [
                {
                    "logRecords": [
                        {
                            "traceId": TID,
                            "spanId": "1" * 16,
                            "attributes": {
                                "gen_ai.evaluation.name": "correctness",
                                "gen_ai.evaluation.score.value": 0.5,
                            },
                        }
                    ]
                }
            ]
        }
    ]
    result = import_otlp(source(tmp_path, [first, later]))
    assert result.inventory()["raw_log_records"] == 1
    assert (
        result.source_receipts[0].to_dict()["source_metadata"]["source_logs"][0]["record"][
            "attributes"
        ]["gen_ai.evaluation.score.value"]
        == 0.5
    )
    assert result.source_receipts[0].to_dict()["outcome"]["quality_pass"] is None


def test_cli_batch_import_and_single_document_mode(tmp_path):
    runner = CliRunner()
    path = source(tmp_path, [document(span())])
    result = runner.invoke(
        app,
        [
            "telemetry",
            "import-jsonl",
            str(path),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "harbor",
            "--synthetic",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 records imported" in result.output
    assert json.loads((tmp_path / "out/inventory.json").read_text())["source_system"] == "harbor"
    single = tmp_path / "single.json"
    single.write_text(json.dumps(document(span()), indent=2), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "telemetry",
            "import-jsonl",
            str(single),
            "--single-json",
            "--out",
            str(tmp_path / "single-out"),
        ],
    )
    assert result.exit_code == 0, result.output
    invalid = runner.invoke(
        app,
        ["telemetry", "import-jsonl", str(path), "--source", "bad", "--out", str(tmp_path / "bad")],
    )
    assert invalid.exit_code != 0
