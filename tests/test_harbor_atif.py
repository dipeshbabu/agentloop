"""ATIF behavior and adversarial import contracts without Harbor or network."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, app
from agentloop.graph import ExecutionGraph
from agentloop.html_report import analysis_to_html
from agentloop.integrations.harbor.atif import AtifOptions, import_atif
from agentloop.interoperability.evidence import EXTERNAL_KEY
from agentloop.interoperability.validation import ImportLimits, ImportValidationError
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures/external/harbor"


def payload(name="atif_v17_simple.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def source(tmp_path, value, name="trajectory.json"):
    file = tmp_path / name
    file.write_text(json.dumps(value), encoding="utf-8")
    return file


@pytest.mark.parametrize(
    "name",
    [
        "atif_v17_simple.json",
        "atif_v18_multimodal.json",
        "atif_aggregated_llm_calls.json",
        "atif_deterministic_dispatch.json",
        "atif_embedded_subagents.json",
        "atif_continuation.json",
    ],
)
def test_frozen_formats_round_trip_into_qualified_native_analysis(name):
    result = import_atif(FIXTURES / name)
    assert result.traces
    for trace in result.traces:
        restored = AgentTrace.from_dict(trace.to_dict())
        assert restored.to_dict() == trace.to_dict()
        analysis = _analysis_payload(restored)
        assert analysis["report"]["total_runtime_ms"] is None
        assert analysis["optimization"]["estimated_after"]["runtime_ms"] is None
        assert analysis["report"]["estimated_cost_usd"] is None
        assert analysis["report"]["analysis_complete"] is True
        html = analysis_to_html(analysis)
        assert "unavailable" in html and "External execution evidence" in html
        assert not restored.metadata.get("success")
        with pytest.raises(ValueError, match="complete external trial"):
            build_replay_report(restored, restored)


def test_unknown_duration_status_and_cost_never_become_measured_zero(tmp_path, monkeypatch):
    value = payload()
    value["agent"]["model_name"] = "gpt-4.1"
    value["steps"][1]["metrics"]["cost_usd"] = 0.25
    monkeypatch.setattr(
        "agentloop.metrics.estimate_cost",
        lambda *args, **kwargs: pytest.fail("external usage was priced"),
    )
    result = import_atif(source(tmp_path, value))
    trace = result.traces[0]
    report = trace.report()
    assert report["input_tokens"] == 12 and report["output_tokens"] == 6
    assert report["token_status"] == "external_reported"
    assert report["model_time_ms"] is None and report["cumulative_span_time_ms"] is None
    assert report["events"][1]["duration_ms"] is None
    assert report["events"][1]["status"] == "unknown"
    assert report["cost_breakdown"]["cost_status"] == "unknown"
    assert (
        report["cost_breakdown"]["model_calls"][0]["unknown_reason"]
        == "external_usage_not_provider_accounting"
    )
    receipt = result.source_receipts[0].to_dict()
    assert receipt["source_metadata"]["steps"][1]["metrics"]["cost_usd"] == 0.25
    assert receipt["source_metadata"]["agent"]["model_name"] == "gpt-4.1"
    assert receipt["outcome"]["quality_pass"] is None
    graph = ExecutionGraph.from_trace(trace)
    assert graph.total_runtime_ms() is None
    assert graph.to_dict()["critical_path"]["duration_ms"] is None
    assert not graph.to_dict()["edges"]
    with pytest.raises(ValueError, match="measured external"):
        graph.critical_path()


def test_sparse_usage_remains_null_in_analysis(tmp_path):
    value = payload()
    value["steps"][1]["metrics"] = {"prompt_tokens": 12}
    trace = import_atif(source(tmp_path, value)).traces[0]
    report = trace.report()
    assert report["input_tokens"] == 12 and report["output_tokens"] is None
    assert report["known_output_tokens"] == 0
    assert report["token_status"] == "partial"
    assert not any(
        card["rule_id"] == "route_to_smaller_model" for card in report["finding_candidates"]
    )
    del value["steps"][1]["metrics"]
    trace = import_atif(source(tmp_path, value)).traces[0]
    assert trace.report()["input_tokens"] is None
    assert trace.report()["output_tokens"] is None


def test_multiplicity_and_deterministic_dispatch_do_not_fabricate_inferences():
    aggregated = import_atif(FIXTURES / "atif_aggregated_llm_calls.json").traces[0]
    models = [event for event in aggregated.events if event.event_type == "model_call"]
    assert len(models) == 1
    assert models[0].metadata["llm_call_count"] == 3
    assert aggregated.report()["reported_model_call_count"] == 3
    assert aggregated.report()["input_tokens"] == 12
    deterministic = import_atif(FIXTURES / "atif_deterministic_dispatch.json").traces[0]
    assert deterministic.report()["model_call_count"] == 0
    assert deterministic.report()["reported_model_call_count"] == 0
    assert len([event for event in deterministic.events if event.event_type == "tool_call"]) == 1


def test_shared_sessions_distinct_documents_and_source_aggregates_do_not_double_count():
    result = import_atif(FIXTURES / "atif_embedded_subagents.json")
    assert len(result.traces) == 3
    assert len({trace.run_id for trace in result.traces}) == 3
    assert sum(trace.report()["input_tokens"] for trace in result.traces) == 36
    receipts = [receipt.to_dict() for receipt in result.source_receipts]
    assert len({receipt["external_identity"]["session_id"] for receipt in receipts}) == 1
    parent = next(
        receipt for receipt in receipts if receipt["external_identity"]["trajectory_id"] == "parent"
    )
    assert parent["source_metadata"]["source_final_metrics"]["total_prompt_tokens"] == 36
    assert len(parent["relationships"]) == 2 and all(
        item["resolved"] for item in parent["relationships"]
    )
    assert all(event.parent_id is None for trace in result.traces for event in trace.events)


def test_copied_steps_are_retained_without_new_execution():
    result = import_atif(FIXTURES / "atif_continuation.json")
    receipt = next(
        receipt.to_dict()
        for receipt in result.source_receipts
        if receipt.to_dict()["external_identity"]["trajectory_id"] == "continued"
    )
    assert receipt["source_metadata"]["copied_context_steps"] == 1
    assert receipt["source_metadata"]["steps"][0]["is_copied_context"] is True
    trace = next(
        trace
        for trace in result.traces
        if trace.metadata["external_identity"]["trajectory_id"] == "continued"
    )
    assert [event.metadata["source_step_id"] for event in trace.events] == [2]


def test_sensitive_content_and_media_are_not_exported_or_read_by_default(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("remote content was read")
    )
    result = import_atif(FIXTURES / "atif_v18_multimodal.json")
    serialized = json.dumps([trace.to_dict() for trace in result.traces]) + json.dumps(
        [receipt.to_dict() for receipt in result.source_receipts]
    )
    assert "SYNTHETIC_REASONING_MUST_BE_OMITTED" not in serialized
    assert "Read the card" not in serialized
    assert "https://example.invalid/instruction.wav" not in serialized
    assert "source_duration_sec" in serialized
    receipt = result.source_receipts[0].to_dict()
    assert receipt["source_metadata"]["steps"][1]["reasoning_content"]["capture"] == "omitted"
    assert receipt["source_metadata"]["steps"][1]["metrics"]["prompt_token_ids"] == {
        "count": 3,
        "capture": "omitted",
    }
    captured = import_atif(
        FIXTURES / "atif_v18_multimodal.json", options=AtifOptions(capture_content=True)
    )
    assert (
        captured.source_receipts[0].to_dict()["source_metadata"]["steps"][0]["message"][0]["text"]
        == "Read the card and listen to the instruction."
    )
    assert (
        captured.source_receipts[0].to_dict()["source_metadata"]["steps"][1]["reasoning_content"][
            "capture"
        ]
        == "omitted"
    )
    explicit = import_atif(
        FIXTURES / "atif_v18_multimodal.json",
        options=AtifOptions(capture_reasoning=True, capture_token_ids=True),
    )
    assert explicit.source_receipts[0].to_dict()["source_metadata"]["steps"][1]["metrics"][
        "prompt_token_ids"
    ] == [1, 2, 3]


@pytest.mark.parametrize(
    "mutation",
    [
        "future_version",
        "duplicate_step",
        "duplicate_document",
        "unknown_source",
        "negative_tokens",
        "boolean_tokens",
        "fractional_tokens",
        "cache_exceeds_input",
        "deterministic_with_metrics",
        "unknown_observation_call",
        "session_only_reference",
        "invalid_timestamp",
        "audio_in_v17",
        "missing_agent_name",
    ],
)
def test_invalid_structural_and_semantic_inputs_reject_deterministically(tmp_path, mutation):
    value = payload()
    if mutation == "future_version":
        value["schema_version"] = "ATIF-v9.9"
    elif mutation == "duplicate_step":
        value["steps"][1]["step_id"] = 1
    elif mutation == "duplicate_document":
        value = payload("atif_embedded_subagents.json")
        value["subagent_trajectories"][1]["trajectory_id"] = "child-a"
    elif mutation == "unknown_source":
        value["steps"][0]["source"] = "vendor"
    elif mutation in {"negative_tokens", "boolean_tokens", "fractional_tokens"}:
        value["steps"][1]["metrics"]["prompt_tokens"] = {
            "negative_tokens": -1,
            "boolean_tokens": True,
            "fractional_tokens": 1.5,
        }[mutation]
    elif mutation == "cache_exceeds_input":
        value["steps"][1]["metrics"]["cached_tokens"] = 100
    elif mutation == "deterministic_with_metrics":
        value["steps"][1]["llm_call_count"] = 0
    elif mutation == "unknown_observation_call":
        value["steps"][1]["observation"] = {"results": [{"source_call_id": "absent"}]}
    elif mutation == "session_only_reference":
        value["steps"][1]["observation"] = {
            "results": [{"subagent_trajectory_ref": [{"session_id": "shared-session"}]}]
        }
    elif mutation == "invalid_timestamp":
        value["steps"][1]["timestamp"] = "SECRET_TIMESTAMP"
    elif mutation == "audio_in_v17":
        value = payload("atif_v18_multimodal.json")
        value["schema_version"] = "ATIF-v1.7"
    else:
        value["agent"]["name"] = None
    with pytest.raises(ImportValidationError) as exc:
        import_atif(source(tmp_path, value))
    assert "SECRET_TIMESTAMP" not in str(exc.value)


def test_invalid_document_can_be_retained_as_a_failed_import_receipt(tmp_path):
    value = payload()
    value["schema_version"] = "ATIF-v2.0"
    result = import_atif(source(tmp_path, value), options=AtifOptions(strict=False))
    assert result.traces == ()
    assert result.inventory()["invalid_documents"] == 1
    receipt = result.source_receipts[0].to_dict()
    assert receipt["source"]["format_version"] == "ATIF-v2.0"
    assert receipt["missing_trace_reason"] == "invalid_artifact"
    assert receipt["outcome"]["quality_pass"] is None


def test_malformed_primary_and_children_remain_source_receipts(tmp_path):
    file = tmp_path / "broken.json"
    file.write_bytes(b"{MALFORMED")
    result = import_atif(file, options=AtifOptions(strict=False))
    assert result.inventory()["invalid_documents"] == 1
    assert (
        result.source_receipts[0].to_dict()["source"]["artifact_sha256"]
        == sha256(file.read_bytes()).hexdigest()
    )
    parent = payload()
    parent["continued_trajectory_ref"] = "broken.json"
    result = import_atif(source(tmp_path, parent))
    assert result.inventory()["invalid_documents"] == 1
    assert result.inventory()["receipts"] == 2
    embedded = payload("atif_embedded_subagents.json")
    embedded["subagent_trajectories"][0]["steps"][1]["metrics"]["prompt_tokens"] = -1
    result = import_atif(source(tmp_path, embedded))
    assert result.inventory()["invalid_documents"] == 1
    assert len(result.traces) == 2
    assert result.source_receipts[-1].to_dict()["completeness"]["trajectory"] == "partial"


def test_privacy_options_remain_separate_and_coverage_labels_survive(tmp_path):
    value = payload()
    value["extra"] = {
        "api_key": "TEST_SECRET",
        "reasoning_content": "TEST_REASONING",
        "prompt_token_ids": [91, 92],
        "usage_scope": "includes_subagents",
    }
    result = import_atif(source(tmp_path, value), options=AtifOptions(capture_content=True))
    serialized = json.dumps([receipt.to_dict() for receipt in result.source_receipts])
    assert "TEST_SECRET" not in serialized and "TEST_REASONING" not in serialized
    assert (
        result.source_receipts[0].to_dict()["source_metadata"]["extra"]["prompt_token_ids"][
            "capture"
        ]
        == "omitted"
    )
    assert (
        result.source_receipts[0].to_dict()["source_metadata"]["extra"]["usage_scope"]
        == "includes_subagents"
    )
    tagged = import_atif(
        FIXTURES / "atif_v17_simple.json", options=AtifOptions(synthetic_fixture=True)
    )
    assert tagged.traces[0].metadata["synthetic"] is True
    assert "Synthetic trace" in analysis_to_html(_analysis_payload(tagged.traces[0]))


def test_frozen_legacy_and_source_interval_variants():
    legacy = import_atif(FIXTURES / "atif_v16_legacy.json")
    assert (
        legacy.source_receipts[0].to_dict()["identity_provenance"]["trajectory_id"] == "calculated"
    )
    assert legacy.traces[0].report()["reported_model_call_count"] is None
    timed = import_atif(FIXTURES / "atif_source_intervals.json")
    assert timed.traces[0].report()["total_runtime_ms"] == 1000


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.json",
        "/absolute.json",
        "C:/private.json",
        "https://example.invalid/a.json",
        "absent.json",
    ],
)
def test_unsafe_or_missing_continuations_stay_explicit_and_unresolved(tmp_path, reference):
    value = payload()
    value["continued_trajectory_ref"] = reference
    result = import_atif(source(tmp_path, value))
    receipt = result.source_receipts[0].to_dict()
    assert receipt["completeness"]["trajectory"] == "partial"
    assert receipt["relationships"][0]["resolved"] is False
    assert len(result.traces) == 1
    assert any(notice["code"] in {"unsafe_path", "missing_artifact"} for notice in result.warnings)


def test_reference_cycles_are_reported_without_recursion_or_fake_edges(tmp_path):
    first, second = payload(), payload()
    first["continued_trajectory_ref"] = "second.json"
    second["trajectory_id"] = "second"
    second["continued_trajectory_ref"] = "trajectory.json"
    source(tmp_path, second, "second.json")
    result = import_atif(source(tmp_path, first))
    assert len(result.traces) == 2
    assert any(notice["code"] == "reference_cycle" for notice in result.warnings)
    assert all(
        receipt.to_dict()["completeness"]["trajectory"] == "partial"
        for receipt in result.source_receipts
    )


def test_bounds_apply_to_documents_events_and_reference_graphs(tmp_path):
    file = source(tmp_path, payload("atif_embedded_subagents.json"))
    for limits in (
        replace(ImportLimits(), max_trajectories=2),
        replace(ImportLimits(), max_events_per_trace=1),
        replace(ImportLimits(), max_json_bytes=64),
        replace(ImportLimits(), max_references=1),
    ):
        with pytest.raises(ImportValidationError):
            import_atif(file, limits=limits)


def test_root_and_file_symlinks_do_not_escape_the_reference_boundary(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    file = source(tmp_path, payload())
    with pytest.raises(ImportValidationError, match="outside the source root"):
        import_atif(file, root=allowed)
    try:
        (allowed / "linked.json").symlink_to(file)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(ImportValidationError) as exc:
        import_atif(allowed / "linked.json")
    assert exc.value.code == "unsafe_path"


def test_repeated_exports_are_stable_and_caller_mutation_cannot_invalidate_receipts(tmp_path):
    first = import_atif(FIXTURES / "atif_v17_simple.json")
    second = import_atif(FIXTURES / "atif_v17_simple.json")
    out = tmp_path / "bundle"
    first.write(out)
    before = {path.relative_to(out): path.read_bytes() for path in out.rglob("*.json")}
    second.write(out)
    assert before == {path.relative_to(out): path.read_bytes() for path in out.rglob("*.json")}
    receipt = first.source_receipts[0].to_dict()
    trace_file = out / receipt["traces"][0]["trace_file"]
    assert sha256(trace_file.read_bytes()).hexdigest() == receipt["traces"][0]["trace_sha256"]
    first.traces[0].events[0].name = "changed"
    with pytest.raises(ImportValidationError, match="changed after receipt"):
        first.write(out)
    assert before[trace_file.relative_to(out)] == trace_file.read_bytes()


def test_job_scopes_and_missing_legacy_ids_have_explicit_stable_identity(tmp_path):
    value = payload()
    value["schema_version"] = "ATIF-v1.6"
    del value["trajectory_id"]
    del value["steps"][1]["llm_call_count"]
    file = source(tmp_path, value)
    first = import_atif(file, identity={"job_id": "job-a", "trial_id": "same"})
    second = import_atif(file, identity={"job_id": "job-b", "trial_id": "same"})
    assert first.traces[0].run_id != second.traces[0].run_id
    assert (
        first.source_receipts[0].to_dict()["identity_provenance"]["trajectory_id"] == "calculated"
    )
    assert first.traces[0].report()["reported_model_call_count"] is None


def test_source_interval_extension_preserves_recorded_measurements_and_failures(tmp_path):
    value = payload()
    timing = {
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
    }
    value["extra"] = {"agentloop": {"timing": timing, "execution_status": "failed"}}
    for step in value["steps"]:
        step["extra"] = {"agentloop": {"timing": timing, "execution_status": "failed"}}
    result = import_atif(source(tmp_path, value))
    assert result.traces[0].report()["total_runtime_ms"] == 1000
    assert result.traces[0].report()["model_time_ms"] == 1000
    assert result.source_receipts[0].to_dict()["outcome"]["execution_status"] == "failed"
    assert all(event.status == "error" for event in result.traces[0].events)
    value["steps"][1]["extra"]["agentloop"]["timing"] = {**timing, "duration_ms": 1}
    with pytest.raises(ImportValidationError, match="contradicts"):
        import_atif(source(tmp_path, value))


def test_canonical_count_findings_abstain_from_unknown_savings(tmp_path):
    value = payload()
    value["steps"] = [
        {
            "step_id": i,
            "source": "agent",
            "message": "dispatch",
            "llm_call_count": 0,
            "tool_calls": [
                {
                    "tool_call_id": f"call-{i}",
                    "function_name": "lookup",
                    "arguments": {"PRIVATE_ARGUMENT_KEY": "PRIVATE_ARGUMENT_VALUE"},
                }
            ],
        }
        for i in range(1, 13)
    ]
    result = import_atif(source(tmp_path, value))
    analysis = _analysis_payload(result.traces[0])
    findings = analysis["diagnosis"]["findings"]
    assert any(finding["rule_id"] == "runaway_loop" for finding in findings)
    assert all(finding["savings"]["estimated_latency_savings_ms"] is None for finding in findings)
    assert all(finding["evidence_level"] == "external_reported" for finding in findings)
    assert all(finding["estimate"]["inputs"]["sum_duration_ms"] is None for finding in findings)
    serialized = json.dumps(analysis)
    assert "PRIVATE_ARGUMENT_KEY" not in serialized and "PRIVATE_ARGUMENT_VALUE" not in serialized
    assert "unavailable" in analysis_to_html(analysis)


def test_missing_or_corrupt_qualifications_fail_closed(tmp_path):
    trace = import_atif(FIXTURES / "atif_v17_simple.json").traces[0]
    for malformed in (None, {}, {**trace.metadata[EXTERNAL_KEY], "execution_status": []}):
        clone = AgentTrace.from_dict(trace.to_dict())
        clone.metadata[EXTERNAL_KEY] = malformed
        with pytest.raises(ImportValidationError):
            clone.report()
    del trace.metadata[EXTERNAL_KEY]
    with pytest.raises(ImportValidationError):
        trace.report()


def test_cli_import_and_existing_analyze_work_offline(tmp_path):
    runner = CliRunner()
    out = tmp_path / "bundle"
    result = runner.invoke(
        app, ["harbor", "import-atif", str(FIXTURES / "atif_v17_simple.json"), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    trace = next((out / "traces").glob("*.json"))
    analyzed = runner.invoke(
        app,
        [
            "analyze",
            str(trace),
            "--html",
            str(out / "report.html"),
            "--json-out",
            str(out / "analysis.json"),
        ],
    )
    assert analyzed.exit_code == 0, analyzed.output
    assert "unavailable" in analyzed.output
    assert json.loads((out / "analysis.json").read_text())["report"]["total_runtime_ms"] is None
    inspected = runner.invoke(
        app, ["harbor", "inspect-atif", str(FIXTURES / "atif_v17_simple.json")]
    )
    assert inspected.exit_code == 0 and "timing_unavailable" in inspected.output
    for command in ("diagnose", "optimize"):
        rendered = runner.invoke(
            app, [command, "--path", str(trace), "--out", str(out / (command + ".md"))]
        )
        assert rendered.exit_code == 0, rendered.output
        assert "unavailable latency" in rendered.output
    reported = runner.invoke(app, ["report", str(trace)])
    assert reported.exit_code == 0 and "unavailable" in reported.output


def test_core_help_and_adapter_do_not_import_external_runtimes():
    code = "import sys; import agentloop; from agentloop.integrations.harbor.atif import import_atif; from agentloop.entrypoint import app; assert not any(name.split('.')[0] in {'harbor','omnigent'} for name in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
