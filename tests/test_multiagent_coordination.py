"""Coordination keeps source boundaries, missingness and inclusive usage explicit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop.html_report import coordination_to_html
from agentloop.integrations.harbor.atif import AtifOptions, import_atif
from agentloop.integrations.harbor.trials import import_harbor
from agentloop.integrations.omnigent.telemetry import import_omnigent
from agentloop.interoperability.contracts import ImportReceipt, receipt_id
from agentloop.interoperability.coordination import summarize_coordination
from agentloop.interoperability.otlp_jsonl import OtlpOptions
from agentloop.interoperability.validation import ImportValidationError

FIXTURES = Path(__file__).parent / "fixtures/external"


def write(path, value):
    path.write_bytes((json.dumps(value) + "\n").encode())
    return path


def atif(tmp_path, value=None, **kwargs):
    value = value or json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    return import_atif(write(tmp_path / "trajectory.json", value), **kwargs)


def summary(result):
    return summarize_coordination(result.traces, result.source_receipts)


def span(identity, *, parent=None, start=0, end=10, role="AGENT", attrs=None, error=False):
    values = {"openinference.span.kind": role, "session.id": "owned-session", **(attrs or {})}
    result = {
        "traceId": "a" * 32,
        "spanId": identity * 16,
        "name": role.lower(),
        "startTimeUnixNano": str(1767225600000000000 + start * 1_000_000_000),
        "endTimeUnixNano": str(1767225600000000000 + end * 1_000_000_000),
        "attributes": [
            {
                "key": key,
                "value": {"intValue": str(value)} if type(value) is int else {"stringValue": value},
            }
            for key, value in values.items()
        ],
        "status": {"code": "STATUS_CODE_ERROR" if error else "STATUS_CODE_OK"},
    }
    if parent:
        result["parentSpanId"] = parent * 16
    return result


def omni(tmp_path, spans):
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    return import_omnigent(
        write(tmp_path / "source.json", payload),
        options=OtlpOptions(system="omnigent", synthetic_fixture=True),
    )


def root_timing(value, start, end):
    value["extra"] = {
        "agentloop": {
            "execution_status": "completed",
            "timing": {
                "started_at": f"2026-01-01T00:00:{start:02d}Z",
                "ended_at": f"2026-01-01T00:00:{end:02d}Z",
                "duration_ms": (end - start) * 1000,
            },
        }
    }


def test_embedded_documents_share_session_without_collapsing_or_adding_aggregates(tmp_path):
    result = atif(tmp_path)
    before = [trace.to_dict() for trace in result.traces]
    report = summary(result)
    assert report["handoff_count"] == 2
    assert report["observed_child_count"] == 2
    assert report["unknown_child_count"] == 0
    assert len(report["actors"]) == 3
    assert len({row["source_identity"]["trajectory_id"] for row in report["actors"]}) == 3
    assert report["usage"]["input_tokens"] == 36
    assert report["usage"]["cached_input_tokens"] == 12
    assert (
        sum(
            row["metrics"]["total_prompt_tokens"]
            for row in report["usage"]["raw_inclusive_aggregates"]
        )
        == 60
    )
    assert report["model_call_count"] == 3
    assert report["parallel_overlap_ms"] is None
    assert report["critical_path_ms"] is None
    assert report["handoff_wait_ms"] is None
    assert report["retry_count"] is None
    assert before == [trace.to_dict() for trace in result.traces]


def test_actual_child_intervals_measure_overlap_without_enclosing_parent_time(tmp_path):
    value = json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    root_timing(value, 0, 10)
    for child, interval in zip(value["subagent_trajectories"], ((1, 6), (3, 8))):
        root_timing(child, *interval)
    report = summary(atif(tmp_path, value))
    assert report["parallel_overlap_ms"] == 3000
    assert report["overlap_coverage"]["observed_leaf_actors"] == 2
    assert [row["child_runtime_ms"] for row in report["handoffs"]] == [5000, 5000]
    assert all(row["handoff_wait_ms"] is None for row in report["handoffs"])


def test_missing_child_receipt_is_retained_without_fabricated_runtime(tmp_path):
    result = atif(tmp_path)
    child = next(
        trace
        for trace in result.traces
        if trace.metadata["external_identity"]["trajectory_id"] == "child-a"
    )
    report = summarize_coordination(
        [trace for trace in result.traces if trace is not child], result.source_receipts
    )
    assert report["handoff_count"] == 2
    assert report["observed_child_count"] == 1
    assert report["unknown_child_count"] == 1
    assert report["parallel_overlap_ms"] is None
    assert any(
        row["child_actor_id"] is None and row["child_runtime_ms"] is None
        for row in report["handoffs"]
    )


def test_external_child_file_resolves_by_document_not_artifact_hash(tmp_path):
    value = json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    child = value["subagent_trajectories"].pop(0)
    write(tmp_path / "child.json", child)
    for step in value["steps"]:
        for result in (step.get("observation") or {}).get("results", []):
            for ref in result.get("subagent_trajectory_ref", []):
                if ref.get("trajectory_id") == "child-a":
                    ref["trajectory_path"] = "child.json"
    report = summary(atif(tmp_path, value))
    assert report["observed_child_count"] == 2
    assert report["unknown_child_count"] == 0


def test_omni_span_tree_child_failures_roles_and_observed_overlap(tmp_path):
    result = omni(
        tmp_path,
        [
            span("1", attrs={"gen_ai.agent.name": "implementer", "agent.role": "implementer"}),
            span(
                "2",
                parent="1",
                start=1,
                end=6,
                attrs={"gen_ai.agent.name": "reviewer", "agent.role": "reviewer"},
            ),
            span(
                "3",
                parent="1",
                start=3,
                end=8,
                attrs={"gen_ai.agent.name": "responder"},
                error=True,
            ),
        ],
    )
    report = summary(result)
    assert report["observed_child_count"] == 2
    assert report["child_failure_count"] == 1
    assert report["parallel_overlap_ms"] == 3000
    assert report["critical_path_ms"] == 10000
    assert (
        next(actor for actor in report["actors"] if actor["agent_name"] == "reviewer")[
            "declared_role"
        ]
        == "reviewer"
    )
    assert report["review_overhead_ms"] is None
    assert report["review_quality_delta"] is None
    assert all(actor["task_outcome"]["quality_pass"] is None for actor in report["actors"])


def test_tool_between_parent_and_child_does_not_double_count_enclosing_agent(tmp_path):
    report = summary(
        omni(
            tmp_path,
            [
                span("1"),
                span("2", parent="1", role="TOOL"),
                span("3", parent="2", start=1, end=5),
                span("4", parent="2", start=3, end=7),
            ],
        )
    )
    assert report["parallel_overlap_ms"] == 2000
    assert report["overlap_coverage"]["observed_leaf_actors"] == 2
    assert all(row["parent_actor_id"] is not None for row in report["handoffs"])


def test_cross_trace_span_link_remains_noncausal_even_when_labeled_delegation():
    result = import_omnigent(FIXTURES / "omnigent/parent_child_multi_trace.otlp.json")
    report = summary(result)
    assert len(report["actors"]) == 2
    assert report["handoff_count"] == 0
    assert report["critical_path_ms"] is None
    assert all(row["causal_edge"] is False for row in report["relationships"])
    assert report["parallel_overlap_ms"] == 1000


@pytest.mark.parametrize("parent_session", ["owned-parent-session", "missing-parent-session"])
def test_parent_identity_correlates_without_creating_dispatch_edges(tmp_path, parent_session):
    result = omni(
        tmp_path,
        [
            span("1", attrs={"agent.id": "owned-parent", "session.id": "owned-parent-session"}),
            span(
                "2",
                attrs={
                    "agent.id": "owned-child",
                    "parent.agent.id": "owned-parent",
                    "parent.session.id": parent_session,
                },
            ),
        ],
    )
    report = summary(result)
    reference = report["relationships"][0]
    assert reference["kind"] == "declared_parent_identity"
    assert reference["resolved"] is (parent_session == "owned-parent-session")
    assert reference["causal_edge"] is False
    assert report["handoff_count"] == 0
    assert report["critical_path_ms"] is None


def test_missing_child_timing_never_becomes_zero_duration(tmp_path):
    child = span("2", parent="1")
    child.pop("endTimeUnixNano")
    report = summary(omni(tmp_path, [span("1"), child]))
    assert report["actors"][1]["runtime_ms"] is None
    assert report["parallel_overlap_ms"] is None
    assert report["critical_path_ms"] is None


def test_model_parent_aggregate_is_excluded_from_leaf_attribution(tmp_path):
    report = summary(
        omni(
            tmp_path,
            [
                span("1", attrs={"gen_ai.usage.input_tokens": 100}),
                span(
                    "2",
                    parent="1",
                    role="LLM",
                    attrs={"gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 10},
                ),
                span(
                    "3",
                    parent="2",
                    role="LLM",
                    attrs={"gen_ai.usage.input_tokens": 20, "gen_ai.usage.output_tokens": 5},
                ),
            ],
        )
    )
    assert report["usage"]["known_input_tokens"] == 20
    assert report["usage"]["input_tokens"] is None
    assert report["observed_model_blocks"] == 2
    assert report["usage"]["provider_billing_verified"] is False


def test_equal_tool_names_without_captured_inputs_cannot_prove_repeated_work(tmp_path):
    report = summary(
        omni(
            tmp_path,
            [span("1"), span("2", parent="1", role="TOOL"), span("3", parent="1", role="TOOL")],
        )
    )
    assert report["possible_duplicate_work_count"] is None
    assert report["known_possible_duplicate_work_count"] == 0
    assert report["possible_duplicate_work"] == []


@pytest.mark.parametrize("same_arguments", [True, False])
def test_identical_explicitly_captured_tool_inputs_are_investigation_only(tmp_path, same_arguments):
    value = json.loads((FIXTURES / "harbor/atif_v17_simple.json").read_text())
    step = value["steps"][1]
    step["tool_calls"] = [
        {
            "tool_call_id": "first",
            "function_name": "lookup",
            "arguments": {"query": "PRIVATE_ARGUMENT"},
        },
        {
            "tool_call_id": "second",
            "function_name": "lookup",
            "arguments": {"query": "PRIVATE_ARGUMENT" if same_arguments else "different"},
        },
    ]
    step["observation"] = {
        "results": [
            {"source_call_id": call["tool_call_id"], "content": "PRIVATE_RESULT"}
            for call in step["tool_calls"]
        ]
    }
    report = summary(atif(tmp_path, value, options=AtifOptions(capture_content=True)))
    assert report["possible_duplicate_work_count"] == int(same_arguments)
    assert "PRIVATE_ARGUMENT" not in json.dumps(report)
    assert "PRIVATE_RESULT" not in json.dumps(report)
    if same_arguments:
        finding = report["possible_duplicate_work"][0]
        assert finding["estimated_savings_ms"] is None
        assert finding["estimated_savings_usd"] is None
        assert finding["safe_reuse"] == "unestablished"


def test_mutated_projection_and_duplicate_receipt_binding_fail(tmp_path):
    result = atif(tmp_path)
    with pytest.raises(ImportValidationError):
        summarize_coordination(result.traces, (*result.source_receipts, result.source_receipts[0]))
    result.traces[0].events[0].duration_ms = 999
    with pytest.raises(ImportValidationError):
        summary(result)


def test_exports_reuse_html_styles_escape_content_and_are_idempotent(tmp_path):
    result = omni(
        tmp_path,
        [
            span(
                "1",
                attrs={"gen_ai.agent.name": "<script>alert(1)</script>", "agent.role": "reviewer"},
            )
        ],
    )
    result.write(tmp_path / "out")
    before = (tmp_path / "out/coordination.json").read_bytes()
    result.write(tmp_path / "out")
    assert (tmp_path / "out/coordination.json").read_bytes() == before
    html = (tmp_path / "out/coordination.html").read_text()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Content-Security-Policy" in html
    assert "Synthetic inputs" in html
    assert "reviewer" in html
    assert html == coordination_to_html(summary(result))


def test_atif_roles_are_explicit_and_do_not_establish_correctness(tmp_path):
    value = json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    value["subagent_trajectories"][0]["agent"]["extra"] = {"role": "reviewer"}
    report = summary(atif(tmp_path, value))
    actor = next(
        actor
        for actor in report["actors"]
        if actor["source_identity"]["trajectory_id"] == "child-a"
    )
    assert actor["declared_role"] == "reviewer"
    assert actor["task_outcome"]["quality_pass"] is None


def test_cyclic_parents_prevent_critical_path_claim(tmp_path):
    report = summary(omni(tmp_path, [span("1", parent="2"), span("2", parent="1")]))
    assert report["critical_path_ms"] is None


def test_repeated_trajectory_ids_in_distinct_jobs_keep_separate_children(tmp_path):
    results = []
    for name in ("job-a", "job-b"):
        folder = tmp_path / name
        folder.mkdir()
        results.append(atif(folder, identity={"job_id": name, "trial_id": "owned-trial"}))
    report = summarize_coordination(
        [trace for result in results for trace in result.traces],
        [receipt for result in results for receipt in result.source_receipts],
    )
    assert len(report["actors"]) == 6
    assert report["observed_child_count"] == 4
    assert report["unknown_child_count"] == 0


def test_missing_model_usage_retains_known_counts_without_inclusive_fallback(tmp_path):
    value = json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    value["subagent_trajectories"][0]["steps"][1].pop("metrics")
    report = summary(atif(tmp_path, value))
    assert report["usage"]["input_tokens"] is None
    assert report["usage"]["known_input_tokens"] == 24
    assert len(report["usage"]["raw_inclusive_aggregates"]) == 3


def test_conflicting_source_segments_cannot_establish_actor_timing(tmp_path):
    first = {"resourceSpans": [{"scopeSpans": [{"spans": [span("1")]}]}]}
    second = {"resourceSpans": [{"scopeSpans": [{"spans": [span("1", end=9)]}]}]}
    path = tmp_path / "conflict.jsonl"
    path.write_bytes((json.dumps(first) + "\n" + json.dumps(second) + "\n").encode())
    report = summary(import_omnigent(path, options=OtlpOptions(system="omnigent")))
    assert report["actors"][0]["runtime_ms"] is None
    assert report["parallel_overlap_ms"] is None
    assert report["critical_path_ms"] is None


def test_coordination_aggregate_projection_does_not_copy_captured_extra_text(tmp_path):
    value = json.loads((FIXTURES / "harbor/atif_embedded_subagents.json").read_text())
    value["final_metrics"]["extra"]["private_note"] = "PRIVATE_AGGREGATE_TEXT"
    report = summary(atif(tmp_path, value, options=AtifOptions(capture_content=True)))
    assert "PRIVATE_AGGREGATE_TEXT" not in json.dumps(report)
    assert any(
        row["metrics"].get("usage_scope") == "includes_subagents"
        for row in report["usage"]["raw_inclusive_aggregates"]
    )


def test_missing_projection_receipts_still_require_unambiguous_trial_identity():
    result = import_harbor(FIXTURES / "harbor/mixed_job")
    receipt = next(row for row in result.trial_receipts if not row.to_dict()["traces"])
    with pytest.raises(ImportValidationError):
        summarize_coordination([], [receipt, receipt])
    second = receipt.to_dict()
    second["source"]["artifact_reference"] += ".duplicate"
    second["receipt_id"] = receipt_id(second)
    with pytest.raises(ImportValidationError, match="ambiguous parent trial identity"):
        summarize_coordination([], [receipt, ImportReceipt.from_dict(second)])
