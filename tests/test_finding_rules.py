from __future__ import annotations

from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.optimizer import OptimizationCard, build_optimization_plan
from agentloop.rules import BUILTIN_RULES, AnalysisContext, FindingCandidate, FindingRule, run_rules


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
    assert [item["rule_id"] for item in candidates] == [
        item["rule_id"] for item in diagnosis["findings"]
    ]
    assert all(item["rule_version"] == "1.0" for item in candidates)
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
            "message": "bad <input>|row",
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
