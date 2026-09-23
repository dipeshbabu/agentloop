from __future__ import annotations

import pytest
from test_semantic_waste import investigation, judge, trace_fixture

from agentloop import AgentTrace, build_diagnosis
from agentloop.entrypoint import _analysis_payload, app
from agentloop.findings import diagnosis_to_markdown
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.plan_export import export_optimization_markdown
from agentloop.semantic_waste import evaluate_semantic_waste
from agentloop.store import SQLiteTraceStore


def findings(trace):
    return [
        item for item in build_diagnosis(trace)["findings"] if item["type"] == "semantic_redundancy"
    ]


def prepare(*, attributed=False, trace=None):
    trace = trace or trace_fixture()
    case = investigation(removal_attribution_ref="host:removal-safety-v1" if attributed else None)
    evaluate_semantic_waste(trace, [case], judges=[judge()], enabled=True)
    return trace


def test_unattributed_findings_preserve_unknown_savings_and_stable_rule_identity():
    trace = prepare()
    finding = findings(trace)[0]
    assert finding["rule_id"] == "semantic_redundancy" and finding["rule_version"] == "1.0"
    assert finding["confidence"] == finding["severity"] == "low"
    assert finding["savings"]["estimated_latency_savings_ms"] is None
    assert finding["savings"]["estimated_cost_savings_usd"] is None
    assert finding["savings"]["estimated_input_tokens"] is None
    assert finding["validation"]["requires_independent_quality"]
    assert not finding["rewrite"]["patchable"]
    assert finding["estimate"]["estimator_id"] == "semantic_leaf_removal"
    assert finding["observations"]["judgments"][0]["judge"]["implementation"] == "fixture"
    assert finding["observations"]["reference_spans"] == ["reference"]
    assert (
        build_optimization_plan(trace)["savings_aggregation"]["latency_estimate_complete"] is False
    )
    assert trace.report()["rule_errors"] == []


def test_attribution_estimates_only_recorded_serial_leaf_work_with_provenance():
    finding = findings(prepare(attributed=True))[0]
    assert finding["savings"]["estimated_latency_savings_ms"] == 10
    assert finding["savings"]["estimated_cost_savings_usd"] == 0.001
    assert finding["savings"]["estimated_input_tokens"] == 10
    assert finding["savings"]["estimated_output_tokens"] == 2
    assert finding["severity"] == finding["confidence"] == "low"
    estimate = finding["estimate"]
    assert estimate["method"] == "conditional_upper_bound"
    assert estimate["calibrated"] is False
    assert estimate["inputs"]["model_costs"][0]["state"] == "provider_reported"
    assert estimate["inputs"]["removal_attribution_ref"] == "host:removal-safety-v1"


@pytest.mark.parametrize(
    "mode", ["overlap", "nested", "nonmodel", "estimated_tokens", "missing_timing"]
)
def test_unsupported_attribution_never_invents_measurements(mode):
    trace = trace_fixture()
    if mode == "overlap":
        trace.events[2].started_at = "2026-01-01T00:00:00.015000+00:00"
        trace.events[2].ended_at = "2026-01-01T00:00:00.025000+00:00"
    elif mode == "nested":
        trace.events[2].parent_id = "target"
    elif mode == "nonmodel":
        trace.events[1].event_type = "tool_call"
    elif mode == "estimated_tokens":
        trace.events[1].token_provenance = "estimated_words"
    else:
        trace.events[2].started_at = trace.events[2].ended_at
    finding = findings(prepare(attributed=True, trace=trace))[0]
    savings = finding["savings"]
    if mode in {"overlap", "nested", "missing_timing"}:
        assert savings["estimated_latency_savings_ms"] is None
    if mode in {"nested", "nonmodel", "estimated_tokens"}:
        assert savings["estimated_input_tokens"] is None
    if mode in {"nested", "nonmodel"}:
        assert savings["estimated_cost_savings_usd"] is None
    if mode == "estimated_tokens":
        # Actual reported billing is known independently of unavailable exact tokens.
        assert savings["estimated_cost_savings_usd"] == 0.001


def test_context_relevance_does_not_attribute_entire_call_cost_to_context():
    trace = trace_fixture()
    case = investigation("context_relevance", removal_attribution_ref="host:context-reduction")
    evaluate_semantic_waste(trace, [case], judges=[judge()], enabled=True)
    finding = next(
        item for item in build_diagnosis(trace)["findings"] if item["type"] == "context_relevance"
    )
    assert finding["savings"]["estimated_latency_savings_ms"] is None
    assert finding["savings"]["estimated_cost_savings_usd"] is None
    assert finding["savings"]["estimated_input_tokens"] is None


def test_independent_investigation_identity_does_not_alias_different_criteria():
    trace = prepare()
    before = findings(trace)[0]["finding_id"]
    second = investigation(investigation_id="case-2", criteria_ref="quality:other-v1")
    evaluate_semantic_waste(trace, [second], judges=[judge()], enabled=True)
    results = findings(trace)
    assert len(results) == 2 and len({item["finding_id"] for item in results}) == 2
    assert results[0]["finding_id"] == before


def test_unknown_findings_roundtrip_store_and_queue_without_auto_patch(tmp_path):
    trace = prepare()
    store = SQLiteTraceStore(str(tmp_path / "semantic.db"))
    store.save_trace(trace)
    restored = store.get_trace(trace.run_id)
    assert findings(restored) == findings(trace)
    saved = next(item for item in store.list_findings() if item["type"] == "semantic_redundancy")
    assert saved["estimated_latency_savings_ms"] is None
    assert saved["estimated_cost_savings_usd"] is None
    queue = next(
        item for item in store.optimization_queue() if item["type"] == "semantic_redundancy"
    )
    assert queue["estimated_latency_savings_ms"] is None
    assert queue["estimated_cost_savings_usd"] is None
    assert queue["quality_risk"] == "high" and queue["requires_scorer"]
    assert queue["safe_to_auto_patch"] is False
    assert queue["unmodeled_latency_count"] == 1


def test_html_markdown_and_cli_render_unavailable_estimates(tmp_path, monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    monkeypatch.setattr("agentloop.cli.console", Console(width=240))
    trace = prepare()
    html = analysis_to_html(_analysis_payload(trace))
    assert "Semantic investigation and judge provenance" in html
    assert "unavailable" in html
    assert "Estimated latency savings: unavailable" in diagnosis_to_markdown(build_diagnosis(trace))
    markdown = export_optimization_markdown(
        build_optimization_plan(trace), tmp_path / "plan.md"
    ).read_text(encoding="utf-8")
    assert "Estimated latency savings: unavailable" in markdown
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cli.db"))
    store = SQLiteTraceStore(str(tmp_path / "cli.db"))
    store.save_trace(trace)
    for command in ("list-findings", "optimization-queue"):
        result = CliRunner().invoke(app, [command])
        assert result.exit_code == 0, result.output
        assert "unavailable" in result.output


def test_native_json_preserves_investigation_and_no_autoexecution(monkeypatch):
    trace = prepare()
    restored = AgentTrace.from_dict(trace.to_dict())
    from agentloop.judgments import LocalCallbackJudge

    monkeypatch.setattr(
        LocalCallbackJudge,
        "judge",
        lambda *args, **kwargs: pytest.fail("ordinary report must not execute"),
    )
    assert findings(restored) == findings(trace)


def test_semantic_hypotheses_do_not_invent_engineering_or_reliability_value():
    from agentloop.value import build_value_report

    trace = trace_fixture()
    before = build_value_report(trace)
    prepare(trace=trace)
    after = build_value_report(trace)
    assert (
        after["monthly_value"]["engineering_hours_saved"]
        == before["monthly_value"]["engineering_hours_saved"]
    )
    assert after["reliability"]["risk_score"] == before["reliability"]["risk_score"]


def test_http_analysis_preserves_unknown_savings_without_judge_execution(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentloop.server import app as server_app

    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "semantic-http.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    trace = prepare()
    client = TestClient(server_app)
    assert client.post("/v1/traces", json=trace.to_dict()).status_code == 200
    response = client.get(f"/v1/traces/{trace.run_id}/diagnosis")
    assert response.status_code == 200
    finding = next(
        item for item in response.json()["findings"] if item["type"] == "semantic_redundancy"
    )
    assert finding["savings"]["estimated_latency_savings_ms"] is None
    assert finding["observations"]["judgments"][0]["value"] is True


def test_repeated_cases_share_attribution_indexes_and_timing_scan(monkeypatch):
    import agentloop.semantic_rules as module

    trace = trace_fixture()
    cases = [
        investigation(investigation_id=f"case-{index}", removal_attribution_ref="host:removal")
        for index in range(12)
    ]
    evaluate_semantic_waste(trace, cases, judges=[judge()], enabled=True)
    calls = []
    original = module.event_interval_ms

    def counted(event):
        calls.append(event.event_id)
        return original(event)

    monkeypatch.setattr(module, "event_interval_ms", counted)
    trace.report()
    assert sorted(calls) == ["downstream", "reference", "target"]


def test_estimated_token_pricing_is_not_promoted_to_exact_avoidable_cost():
    trace = trace_fixture()
    target = trace.events[1]
    target.metadata.pop("provider_reported_cost_usd")
    target.model = "gpt-4o-mini"
    target.token_provenance = "estimated_words"
    assert trace.report()["cost_breakdown"]["model_calls"][1]["state"] == "calculated"
    finding = findings(prepare(attributed=True, trace=trace))[0]
    assert finding["savings"]["estimated_cost_savings_usd"] is None
    assert finding["savings"]["estimated_input_tokens"] is None


def test_zero_duration_disagreeing_with_timestamps_is_not_known_zero_savings():
    trace = trace_fixture()
    trace.events[1].duration_ms = 0
    finding = findings(prepare(attributed=True, trace=trace))[0]
    assert finding["savings"]["estimated_latency_savings_ms"] is None
