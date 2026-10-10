"""Source-qualified sessions/policy observations without an Omnigent runtime."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, app
from agentloop.html_report import analysis_to_html
from agentloop.integrations.omnigent.evidence import OMNIGENT_KEY, read_observations
from agentloop.integrations.omnigent.telemetry import import_omnigent, inspect_omnigent_bundle
from agentloop.interoperability.otlp_jsonl import OtlpOptions
from agentloop.interoperability.validation import ImportValidationError
from agentloop.otel import trace_to_otel
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures/external/omnigent"
TID = "a" * 32


def span(
    sid="1" * 16, *, trace=TID, kind="AGENT", start=0, end=10_000_000_000, attrs=None, **extra
):
    return {
        "traceId": trace,
        "spanId": sid,
        "name": "observed",
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "status": {"code": 1},
        "attributes": {"openinference.span.kind": kind, "session.id": "session-1", **(attrs or {})},
        **extra,
    }


def document(*spans):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": {"service.name": "omnigent"}},
                "scopeSpans": [{"scope": {"name": "omnigent"}, "spans": list(spans)}],
            }
        ]
    }


def source(tmp_path, value, name="trace.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "name",
    [
        "agent_tool_policy.otlp.json",
        "parent_child_multi_trace.otlp.json",
        "unverified_policy_decision.otlp.json",
        "missing_parentage.otlp.json",
    ],
)
def test_frozen_formats_native_round_trip_and_qualified_html(name):
    result = import_omnigent(FIXTURES / name)
    assert result.traces
    for trace in result.traces:
        restored = AgentTrace.from_dict(trace.to_dict())
        assert read_observations(restored) == read_observations(trace)
        observed = restored.report()["omnigent_observations"]
        assert observed["model_coverage"] == "unknown"
        assert observed["enforcement"] == "unverified"
        assert observed["task_correctness"] == "unavailable"
        html = analysis_to_html(_analysis_payload(restored))
        assert "Omnigent observations" in html and "does not prove prevention" in html
        with pytest.raises(ValueError, match="complete external trial"):
            build_replay_report(restored, restored)


def test_session_group_does_not_collapse_separate_traces_or_create_causal_edges():
    result = import_omnigent(FIXTURES / "parent_child_multi_trace.otlp.json")
    assert len(result.traces) == 2
    group = result.sessions()["groups"][0]
    assert len(group["run_ids"]) == 2
    assert set(group["source_trace_ids"]) == {TID, "b" * 32}
    relation = group["relationships"][0]
    assert relation["resolved"] is True
    assert relation["causal_edge"] is False
    assert result.traces[1].events[0].parent_id is None


def test_two_child_traces_keep_shared_session_and_original_agent_ids(tmp_path):
    root = span(attrs={"gen_ai.agent.name": "parent", "omnigent.agent.id": "agent-parent"})
    children = [
        span(
            str(index) * 16,
            trace=str(index) * 32,
            attrs={
                "gen_ai.agent.name": f"child-{index}",
                "omnigent.agent.id": f"agent-{index}",
                "omnigent.parent_agent.id": "agent-parent",
                "parent.session.id": "session-1",
            },
            links=[{"traceId": TID, "spanId": "1" * 16}],
        )
        for index in (2, 3)
    ]
    result = import_omnigent(source(tmp_path, document(root, *children)))
    assert len(result.traces) == 3
    assert len(result.sessions()["groups"][0]["run_ids"]) == 3
    for child in result.traces[1:]:
        metadata = child.events[0].metadata["source.omnigent"]
        assert metadata["parent_agent_id"] == "agent-parent"
        assert metadata["parent_session_id"] == "session-1"
        assert child.events[0].parent_id is None


@pytest.mark.parametrize(
    "action,expected",
    [
        ("allow", "ALLOW"),
        ("deny", "DENY"),
        ("ask", "ASK"),
        ("DENY", "DENY"),
        ("future-action", "UNKNOWN"),
    ],
)
def test_policy_verdict_is_observed_without_false_enforcement(tmp_path, action, expected):
    policy = span(
        kind="GUARDRAIL",
        attrs={
            "policy.name": "test-policy",
            "policy.phase": "tool_call",
            "policy.action": action,
            "policy.enforced": True,
        },
        status={"code": 2 if action.lower() == "deny" else 1},
    )
    result = import_omnigent(source(tmp_path, document(policy)))
    decision = read_observations(result.traces[0])["policy_decisions"][0]
    assert decision["action"] == expected and decision["source_action"] == action
    assert decision["enforcement"] == "unverified"
    assert decision["dispatch_prevention"] == "unknown"
    assert decision["dispatch_observation"] == "unknown"
    assert result.source_receipts[0].to_dict()["outcome"]["execution_status"] == "unknown"
    assert result.traces[0].report()["external_evidence"]["execution_status"] == "unknown"


def test_late_deny_with_matching_tool_span_is_not_prevention(tmp_path):
    tool = span(
        kind="TOOL",
        start=0,
        end=1_000_000_000,
        attrs={"gen_ai.tool.call.id": "call-1", "gen_ai.tool.name": "lookup"},
    )
    policy = span(
        "2" * 16,
        kind="GUARDRAIL",
        start=2_000_000_000,
        end=3_000_000_000,
        attrs={
            "policy.action": "deny",
            "policy.phase": "tool_result",
            "policy.tool_call_id": "call-1",
        },
    )
    result = import_omnigent(source(tmp_path, document(tool, policy)))
    decision = read_observations(result.traces[0])["policy_decisions"][0]
    assert decision["dispatch_observation"] == "recorded_tool_span"
    assert decision["observed_tool_event_ids"] == [result.traces[0].events[0].event_id]
    assert decision["dispatch_prevention"] == "unknown"


def test_unresolved_ask_and_recorded_approval_remain_distinct(tmp_path):
    policy = span(kind="GUARDRAIL", attrs={"policy.action": "ask", "approval.id": "approval-1"})
    first = import_omnigent(source(tmp_path, document(policy)))
    assert "unresolved_policy_ask" in read_observations(first.traces[0])["gaps"]
    policy["attributes"]["approval.resolution"] = "allow"
    second = import_omnigent(source(tmp_path, document(policy)))
    observed = read_observations(second.traces[0])
    assert observed["policy_decisions"][0]["approval_resolution"] == "allow"
    assert observed["policy_decisions"][0]["dispatch_prevention"] == "unknown"
    assert "unresolved_policy_ask" not in observed["gaps"]


def test_turn_model_tool_policy_approval_host_times_stay_separate(tmp_path):
    events = [
        span(),
        span(
            "2" * 16,
            kind="LLM",
            start=1_000_000_000,
            end=4_000_000_000,
            attrs={"llm.token_count.prompt": 12, "llm.token_count.completion": 6},
        ),
        span("3" * 16, kind="TOOL", start=5_000_000_000, end=7_000_000_000),
        span(
            "4" * 16,
            kind="GUARDRAIL",
            start=7_000_000_000,
            end=8_000_000_000,
            attrs={"policy.action": "allow"},
        ),
        span("5" * 16, start=0, end=1_000_000_000, attrs={"omnigent.span.role": "approval"}),
        span(
            "6" * 16, start=8_000_000_000, end=10_000_000_000, attrs={"omnigent.span.role": "host"}
        ),
    ]
    result = import_omnigent(source(tmp_path, document(*events)))
    trace = result.traces[0]
    observed = read_observations(trace)
    timing = observed["timing_by_role"]
    assert timing["agent"]["interval_union_ms"] == 10_000
    assert timing["model"]["interval_union_ms"] == 3_000
    assert timing["tool"]["interval_union_ms"] == 2_000
    assert timing["guardrail"]["interval_union_ms"] == 1_000
    assert timing["approval"]["interval_union_ms"] == 1_000
    assert timing["host"]["interval_union_ms"] == 2_000
    assert observed["model_coverage"] == "observed_partial"
    assert trace.report()["input_tokens"] == 12
    assert trace.report()["output_tokens"] == 6
    assert trace.report()["total_runtime_ms"] == 10_000


def test_nested_agent_intervals_are_not_summed_as_elapsed_runtime(tmp_path):
    first = span()
    nested = span("2" * 16, start=2_000_000_000, end=6_000_000_000, parentSpanId="1" * 16)
    trace = import_omnigent(source(tmp_path, document(first, nested))).traces[0]
    timing = read_observations(trace)["timing_by_role"]["agent"]
    assert timing["span_cumulative_ms"] == 14_000
    assert timing["interval_union_ms"] == trace.report()["total_runtime_ms"] == 10_000


def test_ended_parent_attribute_stays_unresolved_without_trace_scope(tmp_path):
    first = span()
    later = span("2" * 16, trace="b" * 32, attrs={"parent_span_id": "1" * 16})
    result = import_omnigent(source(tmp_path, document(first, later)))
    relation = read_observations(result.traces[1])["relationships"][0]
    assert relation["kind"] == "ended_parent_reference"
    assert relation["resolved"] is False
    assert relation["causal_edge"] is False
    assert result.traces[1].events[0].parent_id is None
    assert "cross_process_parentage_incomplete" in read_observations(result.traces[1])["gaps"]


def test_missing_stream_bounds_and_unknown_vendor_features_are_not_success(tmp_path):
    value = span(
        attrs={
            "omnigent.harness": "vendor-sdk",
            "omnigent.harness.version": "fixture-1",
            "omnigent.integration_mode": "sdk-in-process",
            "vendor.supports_interrupt": True,
        }
    )
    value.pop("endTimeUnixNano")
    trace = import_omnigent(source(tmp_path, document(value))).traces[0]
    assert trace.events[0].metadata["source.omnigent"]["harness"] == "vendor-sdk"
    assert read_observations(trace)["enforcement"] == "unverified"
    assert trace.report()["total_runtime_ms"] is None
    assert read_observations(trace)["timing_by_role"]["agent"]["interval_union_ms"] is None


def test_privacy_and_capture_flags_survive_default_json_html(tmp_path):
    value = span(
        kind="GUARDRAIL",
        attrs={
            "policy.action": "ask",
            "policy.reason": "PRIVATE_REASON",
            "input.value": "PRIVATE_INPUT",
            "output.value": "PRIVATE_OUTPUT",
            "error.message": "PRIVATE_ERROR",
            "access_token": "PRIVATE_SECRET",
            "omnigent.capture_content": True,
        },
        status={"code": 2, "message": "PRIVATE_STATUS"},
    )
    trace = import_omnigent(source(tmp_path, document(value))).traces[0]
    assert (
        trace.events[0].metadata["source.omnigent"]["capture_flags"]["omnigent.capture_content"]
        is True
    )
    assert "PRIVATE_" not in json.dumps(trace.to_dict())
    assert "PRIVATE_" not in analysis_to_html(_analysis_payload(trace))


def test_duplicate_reconnect_and_late_span_use_existing_jsonl_merge(tmp_path):
    root = document(span())
    tool = document(span("2" * 16, kind="TOOL", parentSpanId="1" * 16))
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(value) for value in (root, root, tool)) + "\n", encoding="utf-8"
    )
    result = import_omnigent(path)
    assert len(result.traces) == 1 and len(result.traces[0].events) == 2
    assert result.inventory()["identical_duplicate_spans"] == 1
    assert result.traces[0].events[1].parent_id == result.traces[0].events[0].event_id


def test_bound_observations_and_exported_trace_mutation_fail_closed(tmp_path):
    result = import_omnigent(FIXTURES / "agent_tool_policy.otlp.json")
    result.traces[0].metadata[OMNIGENT_KEY]["observations"]["enforcement"] = "verified"
    with pytest.raises(ImportValidationError, match="observation"):
        result.traces[0].report()
    with pytest.raises(ImportValidationError, match="changed after receipt"):
        result.write(tmp_path / "out")


def test_stable_export_and_inspect_keep_all_receipt_hashes(tmp_path):
    first = import_omnigent(FIXTURES / "parent_child_multi_trace.otlp.json")
    second = import_omnigent(FIXTURES / "parent_child_multi_trace.otlp.json")
    assert first.sessions() == second.sessions()
    assert first.inventory() == second.inventory()
    out = tmp_path / "out"
    first.write(out)
    second.write(out)
    assert inspect_omnigent_bundle(out)["sessions"] == first.sessions()
    for receipt in first.source_receipts:
        for ref in receipt.to_dict()["traces"]:
            assert sha256((out / ref["trace_file"]).read_bytes()).hexdigest() == ref["trace_sha256"]


def test_source_metadata_otel_round_trip_has_no_duplicate_prefixes(tmp_path):
    result = import_omnigent(
        source(
            tmp_path,
            document(span(attrs={"gen_ai.agent.name": "fixture-agent", "custom.number": 7})),
        )
    )
    exported = trace_to_otel(result.traces[0])
    assert "agentloop.metadata.agentloop.metadata." not in json.dumps(exported)
    restored = import_omnigent(source(tmp_path, exported, name="roundtrip.json"))
    assert restored.traces[0].events[0].metadata["custom.number"] == 7
    assert read_observations(restored.traces[0])["agent_names"] == ["fixture-agent"]


def test_cli_import_and_exported_session_inspect(tmp_path):
    runner = CliRunner()
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "omnigent",
            "import-otel",
            str(FIXTURES / "parent_child_multi_trace.otlp.json"),
            "--out",
            str(out),
            "--synthetic",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 trace projections" in result.output
    inspected = runner.invoke(app, ["omnigent", "inspect", str(out)])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["inventory"]["session_groups"] == 1
    invalid = runner.invoke(app, ["omnigent", "inspect", str(tmp_path / "missing")])
    assert invalid.exit_code != 0


def test_wrong_source_options_and_unknown_bundle_version_are_rejected(tmp_path):
    with pytest.raises(ImportValidationError, match="omnigent source"):
        import_omnigent(
            FIXTURES / "agent_tool_policy.otlp.json", options=OtlpOptions(system="harbor")
        )
    result = import_omnigent(FIXTURES / "agent_tool_policy.otlp.json")
    out = tmp_path / "out"
    result.write(out)
    value = json.loads((out / "omnigent-sessions.json").read_text())
    value["schema_version"] = "future"
    (out / "omnigent-sessions.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ImportValidationError, match="supported Omnigent"):
        inspect_omnigent_bundle(out)


def test_mutated_session_inventory_is_detected_without_claiming_authenticity(tmp_path):
    result = import_omnigent(FIXTURES / "parent_child_multi_trace.otlp.json")
    out = tmp_path / "out"
    result.write(out)
    value = json.loads((out / "omnigent-sessions.json").read_text())
    value["groups"][0]["run_ids"] = []
    (out / "omnigent-sessions.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ImportValidationError, match="supported Omnigent"):
        inspect_omnigent_bundle(out)


def test_session_identity_is_scoped_by_explicit_job_context():
    first = import_omnigent(
        FIXTURES / "agent_tool_policy.otlp.json",
        options=OtlpOptions(system="omnigent", identity={"job_id": "first"}),
    )
    second = import_omnigent(
        FIXTURES / "agent_tool_policy.otlp.json",
        options=OtlpOptions(system="omnigent", identity={"job_id": "second"}),
    )
    assert first.sessions()["groups"][0]["group_id"] != second.sessions()["groups"][0]["group_id"]


def test_conflicting_source_aliases_do_not_silently_pick_one_agent(tmp_path):
    value = span(attrs={"gen_ai.agent.name": "first", "agent.name": "second"})
    trace = import_omnigent(source(tmp_path, document(value))).traces[0]
    aliases = trace.events[0].metadata["source.omnigent"]
    assert aliases["agent_name"] is None
    assert "agent_name" in aliases["ambiguous_aliases"]
    assert read_observations(trace)["agent_names"] == []


def test_uppercase_link_ids_resolve_without_creating_causal_edges(tmp_path):
    parent = span()
    child = span("2" * 16, trace="b" * 32, links=[{"traceId": TID.upper(), "spanId": "1" * 16}])
    result = import_omnigent(source(tmp_path, document(parent, child)))
    relation = read_observations(result.traces[1])["relationships"][0]
    assert relation["resolved"] is True and relation["causal_edge"] is False


def test_large_observation_inventory_reuses_event_metadata_without_receipt_duplication(tmp_path):
    events = [span(f"{index:016x}", kind="TOOL") for index in range(1, 1001)]
    result = import_omnigent(source(tmp_path, document(*events)))
    assert read_observations(result.traces[0])["observation_count"] == 1000
    assert len(result.traces[0].events) == 1000
    result.write(tmp_path / "large-out")


def test_conflicting_telemetry_segments_do_not_claim_precise_role_latency(tmp_path):
    path = tmp_path / "conflict.jsonl"
    path.write_text(
        "\n".join(json.dumps(document(value)) for value in (span(), span(end=99_000_000_000)))
        + "\n",
        encoding="utf-8",
    )
    trace = import_omnigent(path).traces[0]
    timing = read_observations(trace)["timing_by_role"]["agent"]
    assert timing["interval_union_ms"] is None
    assert timing["coverage"] == "partial"
    assert trace.report()["total_runtime_ms"] is None
