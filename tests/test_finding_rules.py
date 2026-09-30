from __future__ import annotations

import pytest

from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.graph import ExecutionGraph
from agentloop.optimizer import OptimizationCard, build_optimization_plan
from agentloop.rules import BUILTIN_RULES, AnalysisContext, FindingCandidate, FindingRule, run_rules
from agentloop.tracer import AgentTrace, record_model_call


@pytest.mark.parametrize("model", ["gpt-4.1", "local-knn"])
@pytest.mark.parametrize("input_tokens", [0, 100])
def test_unavailable_usage_does_not_recommend_smaller_models(model, input_tokens):
    trace = AgentTrace("missing-usage")
    record_model_call(
        "predict",
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=100,
        model=model,
        input_tokens=input_tokens,
        token_provenance="unavailable",
        trace=trace,
    )
    report = trace.report()
    surfaces = (
        report["finding_candidates"],
        build_optimization_plan(trace, report)["optimization_cards"],
        build_diagnosis(trace)["findings"],
    )
    assert report["analysis_complete"] is True
    for findings in surfaces:
        assert not any(item["type"] == "route_to_smaller_model" for item in findings)


@pytest.mark.parametrize(
    "provenance", ["provider", "tokenizer", "user_supplied", "estimated_words", None]
)
def test_available_usage_still_routes_after_missing_calls(provenance):
    trace = AgentTrace("mixed-usage")
    for index in range(4):
        record_model_call(
            f"predict-{index}",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=100,
            model="gpt-4.1",
            input_tokens=100 if index == 3 else 0,
            token_provenance="unavailable",
            trace=trace,
        )
    trace.events[-1].token_provenance = provenance
    graph = ExecutionGraph.from_trace(trace)
    assert [node.to_dict()["token_provenance"] for node in graph.nodes] == [
        "unavailable",
        "unavailable",
        "unavailable",
        provenance,
    ]
    routes = [
        item
        for item in trace.report()["finding_candidates"]
        if item["type"] == "route_to_smaller_model"
    ]
    assert len(routes) == 1
    assert routes[0]["affected_nodes"] == [trace.events[-1].event_id]


def test_provider_reported_zero_is_distinct_from_unavailable_usage():
    trace = AgentTrace("known-zero")
    record_model_call(
        "predict",
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=100,
        model="gpt-4.1",
        token_provenance="provider",
        trace=trace,
    )
    assert any(
        item["type"] == "route_to_smaller_model" for item in trace.report()["finding_candidates"]
    )


def test_report_optimizer_and_diagnosis_share_rule_identity_and_findings():
    trace = _quickstart_trace()
    report = trace.report()
    plan = build_optimization_plan(trace, report)
    diagnosis = build_diagnosis(trace)
    candidates = report["finding_candidates"]
    assert candidates == plan["optimization_cards"]
    assert [item["title"] for item in candidates] == [
        item["title"] for item in report["recommendations"]
    ]
    assert sorted(item["rule_id"] for item in candidates) == sorted(
        item["rule_id"] for item in diagnosis["findings"]
    )
    versions = {rule.rule_id: rule.version for rule in BUILTIN_RULES}
    assert all(item["rule_version"] == versions[item["rule_id"]] for item in candidates)
    assert (
        report["analysis_complete"]
        is plan["analysis_complete"]
        is diagnosis["analysis_complete"]
        is True
    )
    assert OptimizationCard is FindingCandidate


def test_registry_ids_and_order_are_stable():
    assert [rule.rule_id for rule in BUILTIN_RULES] == [
        "parallelize_tools",
        "cache_context",
        "add_schema_validation",
        "batch_model_calls",
        "route_to_smaller_model",
        "split_large_step",
        "runaway_loop",
        "tool_oscillation",
        "semantic_redundancy",
        "low_contribution",
        "semantic_no_progress",
        "retry_usefulness",
        "context_relevance",
    ]


def test_one_rule_failure_is_isolated_and_observable(monkeypatch):
    import agentloop.rules as rules_module

    def broken(context):
        raise ValueError("bad <input>|row")

    monkeypatch.setattr(
        rules_module, "BUILTIN_RULES", (FindingRule("broken", "2.0", broken), BUILTIN_RULES[0])
    )
    trace = _quickstart_trace()
    report = trace.report()
    assert report["analysis_complete"] is False
    assert report["rule_errors"] == [
        {
            "rule_id": "broken",
            "rule_version": "2.0",
            "error_type": "ValueError",
            "message": "Rule evaluation failed; see local debug logs.",
        }
    ]
    assert [item["rule_id"] for item in report["finding_candidates"]] == ["parallelize_tools"]
    assert report["recommendations"][-1]["title"] == "Analysis incomplete"
    diagnosis = build_diagnosis(trace)
    assert diagnosis["analysis_complete"] is False
    markdown = diagnosis_to_markdown(diagnosis)
    assert "Analysis incomplete" in markdown
    assert "<input>" not in markdown
    from agentloop.ci import build_ci_report, ci_report_to_markdown

    ci = build_ci_report(trace, trace)
    assert ci["replay"]["gates"]["passed"] is True
    assert ci["passed"] is False
    assert "Incomplete analysis" in ci_report_to_markdown(ci)


def test_optimizer_reuses_canonical_results_without_rerunning_rules(monkeypatch):
    import agentloop.optimizer as optimizer_module

    trace = _quickstart_trace()
    report = trace.report()

    def unexpected(*args, **kwargs):
        raise AssertionError("rules ran twice")

    monkeypatch.setattr(optimizer_module, "run_rules", unexpected)
    assert (
        build_optimization_plan(trace, report)["optimization_cards"] == report["finding_candidates"]
    )


def test_old_report_without_candidates_uses_same_registry():
    trace = _quickstart_trace()
    report = trace.report()
    expected = report.pop("finding_candidates")
    assert build_optimization_plan(trace, report)["optimization_cards"] == expected


def test_malformed_rule_results_are_reported_without_dropping_other_rules():
    trace = _quickstart_trace()
    from agentloop.graph import ExecutionGraph

    context = AnalysisContext(trace.report(), ExecutionGraph.from_trace(trace))
    candidates, errors = run_rules(
        context, (FindingRule("invalid", "1.0", lambda ctx: [{}]), BUILTIN_RULES[0])
    )
    assert candidates
    assert errors[0]["error_type"] == "TypeError"
