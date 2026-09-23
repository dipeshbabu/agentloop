from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentloop.estimates import ESTIMATORS, Estimator
from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.optimizer import build_optimization_plan
from agentloop.plan_export import export_optimization_markdown
from agentloop.rules import BUILTIN_RULES, FindingCandidate
from agentloop.semantic_waste_types import QUESTIONS
from agentloop.tracer import AgentTrace


def all_rules_trace():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = AgentTrace(name="all-estimators", started_at=start.isoformat(), elapsed_ms=5000)
    specs = [("tool_call", "search", 0), ("tool_call", "lookup", 0)] * 8
    specs += [("model_call", "extract", 100)] * 3
    specs += [("model_call", "synthesize", 100), ("retry", "repair", 0)]
    for index, (kind, name, tokens) in enumerate(specs):
        trace.add_event(
            AgentEvent(
                event_id=f"span-{index}",
                run_id=trace.run_id,
                event_type=kind,
                name=name,
                started_at=(start + timedelta(milliseconds=index * 200)).isoformat(),
                ended_at=(start + timedelta(milliseconds=index * 200 + 100)).isoformat(),
                duration_ms=100,
                model="gpt-4o" if tokens else None,
                input_tokens=tokens,
                output_tokens=4000 if name == "synthesize" else (100 if tokens else 0),
                input_text="shared context " * 200 if tokens else None,
                token_provenance="user_supplied" if tokens else None,
            )
        )
    trace.ended_at = (start + timedelta(milliseconds=5000)).isoformat()
    return trace


def test_default_estimators_are_self_contained_and_reproduce_predictions():
    trace = all_rules_trace()
    plan = build_optimization_plan(trace)
    cards = plan["optimization_cards"]
    # A trace with no explicit investigations must only activate default rules.
    assert {card["rule_id"] for card in cards} == set(ESTIMATORS)
    assert {rule.rule_id for rule in BUILTIN_RULES} == set(ESTIMATORS) | set(QUESTIONS)
    for card in cards:
        estimate = card["estimate"]
        assert estimate["estimator_id"] == card["rule_id"]
        assert estimate["estimator_version"] == "1.0"
        assert estimate["method"] == "heuristic"
        assert estimate["calibrated"] is False
        assert estimate["confidence"] == card["confidence"]
        assert estimate["assumptions"]
        assert estimate["formula"]
        inputs, parameters = estimate["inputs"], estimate["parameters"]
        if card["type"] == "cache_context":
            expected = inputs["current_cost_usd"] * min(
                parameters["max_cost_fraction"], inputs["repeated_context_ratio"]
            )
            assert card["estimated_cost_savings_usd"] == round(expected, 6)
            assert estimate["unmodeled_metrics"] == ["latency"]
        else:
            expected = (
                inputs["sum_duration_ms"] - inputs["max_duration_ms"]
                if card["type"] == "parallelize_tools"
                else inputs["sum_duration_ms"] * parameters["latency_fraction"]
            )
            assert card["estimated_latency_savings_ms"] == round(expected, 3)
            assert estimate["unmodeled_metrics"] == ["cost"]
    assert plan["savings_aggregation"]["selection_optimal"] is True
    assert plan["savings_aggregation"]["selection_algorithm"]
    diagnosis = build_diagnosis(trace)
    assert [card["estimate"] for card in cards] == [
        finding["estimate"] for finding in diagnosis["findings"]
    ]
    assert all(
        finding["savings"]["formula"] == finding["estimate"]["formula"]
        for finding in diagnosis["findings"]
    )


def test_constants_control_calculation_and_old_snapshots_are_not_recomputed(monkeypatch):
    trace = all_rules_trace()
    original = build_optimization_plan(trace)
    snapshot = json.dumps(original, sort_keys=True)
    monkeypatch.setitem(
        ESTIMATORS,
        "batch_model_calls",
        Estimator(
            "sum_duration_ms * latency_fraction",
            ("new assumption",),
            (("latency_fraction", 0.1),),
            version="2.0",
        ),
    )
    changed = build_optimization_plan(trace)
    card = next(
        card for card in changed["optimization_cards"] if card["type"] == "batch_model_calls"
    )
    assert card["estimated_latency_savings_ms"] == 30
    assert card["estimate"]["estimator_version"] == "2.0"
    assert json.dumps(original, sort_keys=True) == snapshot
    restored = FindingCandidate.from_dict(original["optimization_cards"][0])
    restored.estimate["assumptions"].append("mutation")
    exported = restored.to_dict()
    exported["estimate"]["assumptions"].append("export mutation")
    assert "export mutation" not in restored.estimate["assumptions"]
    assert json.dumps(original, sort_keys=True) == snapshot


@pytest.mark.parametrize("basis", ["unknown_model", "estimated_words"])
def test_estimator_inputs_preserve_cost_completeness_and_token_basis(basis):
    trace = all_rules_trace()
    for event in trace.events:
        if event.event_type == "model_call":
            if basis == "unknown_model":
                event.model = "unpriced-model"
            else:
                event.token_provenance = "estimated_words"
    cards = build_optimization_plan(trace)["optimization_cards"]
    assert cards
    for card in cards:
        inputs = card["estimate"]["inputs"]
        if basis == "unknown_model":
            assert card["estimated_cost_savings_usd"] is None
            assert inputs["current_cost_usd"] is None
            assert inputs["cost_status"] == "unknown"
        else:
            assert inputs["token_status"] == "estimated"
            assert inputs["current_cost_usd"] == trace.report()["estimated_cost_usd"]


def test_human_reports_identify_uncalibrated_hypotheses_for_every_card(tmp_path):
    trace = all_rules_trace()
    plan = build_optimization_plan(trace)
    reports = [
        export_optimization_markdown(plan, tmp_path / "plan.md").read_text(encoding="utf-8"),
        diagnosis_to_markdown(build_diagnosis(trace)),
    ]
    for report in reports:
        assert report.count("uncalibrated; predicted savings") == len(plan["optimization_cards"])
        for rule in BUILTIN_RULES:
            if rule.rule_id in ESTIMATORS:
                assert rule.rule_id in report
        assert "latency_fraction" in report
        assert "0.35" in report
        assert "Assumptions:" in report


@pytest.mark.parametrize("family", sorted(QUESTIONS))
def test_explicit_semantic_estimators_preserve_their_inputs_and_human_evidence(family, tmp_path):
    from test_semantic_waste import investigation, judge, trace_fixture

    from agentloop.semantic_waste import evaluate_semantic_waste

    trace = trace_fixture()
    if family == "retry_usefulness":
        trace.events[1].metadata["retry_of"] = "reference"
    case = investigation(family, removal_attribution_ref="fixture:removal-v1")
    evaluate_semantic_waste(trace, [case], judges=[judge()], enabled=True)
    plan = build_optimization_plan(trace)
    card = next(item for item in plan["optimization_cards"] if item["rule_id"] == family)
    snapshot = json.loads(json.dumps(card["estimate"], allow_nan=False))
    assert snapshot["estimator_id"] == "semantic_leaf_removal"
    assert snapshot["estimator_version"] == card["rule_version"] == "1.0"
    assert snapshot["inputs"]["family"] == family
    assert snapshot["inputs"]["token_status"] == "exact"
    assert snapshot["inputs"]["trace_cost_status"] == "complete"
    assert snapshot["calibrated"] is False
    if family == "context_relevance":
        assert set(snapshot["predictions"].values()) == {None}
        assert snapshot["inputs"]["attribution_eligible"] is False
    else:
        count = 2 if family == "semantic_no_progress" else 1
        assert snapshot["predictions"] == {
            "latency_ms": 10 * count,
            "cost_usd": 0.001 * count,
            "input_tokens": 10 * count,
            "output_tokens": 2 * count,
        }
        assert len(snapshot["inputs"]["model_costs"]) == count
        assert snapshot["inputs"]["attribution_eligible"] is True
    for rendered in (
        export_optimization_markdown(plan, tmp_path / "plan.md").read_text(encoding="utf-8"),
        diagnosis_to_markdown(build_diagnosis(trace)),
    ):
        assert family in rendered and "uncalibrated; predicted savings" in rendered
        assert "quality:route-v1" in rendered
    trace.events[1].input_tokens = 1000
    assert card["estimate"] == snapshot
