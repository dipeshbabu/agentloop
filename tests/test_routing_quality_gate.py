"""A smaller-model routing finding ranks last until a quality check has passed."""

from __future__ import annotations

import hashlib
import json

import pytest

from agentloop.findings import build_diagnosis
from agentloop.ranking import RANKING_KEY, cluster_ranking, finalize_ranking, rank_findings
from agentloop.rules import BUILTIN_RULES
from agentloop.tracer import AgentTrace, record_model_call

CHECK = "smaller_model_quality_check"


def finding(identity, *, kind="parallelize_tools", confidence="high", requirements=()):
    return {
        "finding_id": identity,
        "type": kind,
        "title": identity,
        "rule_id": kind,
        "rule_version": "1",
        "confidence": confidence,
        "evidence_level": "declared",
        "affected_spans": [identity],
        "evidence": [{"span_id": identity, "duration_ms": 100}],
        "savings": {"estimated_latency_savings_ms": 50, "estimated_cost_savings_usd": 0.1},
        "estimate": {
            "estimator_id": "fixture",
            "estimator_version": "1",
            "inputs": {"cost_status": "complete"},
            "unmodeled_metrics": [],
        },
        "ranking_requirements": list(requirements),
    }


def routing(identity="route", **changes):
    return finding(identity, kind="route_to_smaller_model", requirements=(CHECK,), **changes)


def declared(value, source="review:v1"):
    return {"value": value, "kind": "declared", "source_ref": source}


STANDARD = {
    "quality_risk": declared("low"),
    "reversible": declared(True),
    "validation_effort_minutes": declared(10),
    "validation_cost_usd": declared(0.1),
}


def ranked(values, routing_inputs=None):
    return rank_findings(
        values,
        inputs={
            "parallelize_tools": STANDARD,
            "route_to_smaller_model": {**STANDARD, **(routing_inputs or {})},
        },
    )


def test_unchecked_routing_ranks_after_weaker_evidence_backed_findings():
    weak = finding("weak", confidence="low")
    result = ranked([routing(), weak])
    assert [row["finding_id"] for row in result] == ["weak", "route"]
    gate = result[1]["ranking"]
    assert gate["status"] == "needs_evidence_or_review"
    assert gate["reasons"] == [
        "missing_input:" + CHECK,
        "quality_check_required:" + CHECK,
    ]
    assert gate["quality_prerequisites"] == {"required": [CHECK], "unmet": [CHECK]}
    assert gate["order"][0] == "quality_prerequisites_met"
    assert result[1]["priority_score"] == 0


def test_passed_quality_check_restores_normal_ordering():
    weak = finding("weak", confidence="low")
    result = ranked([weak, routing()], {CHECK: declared("passed", "routing-study:answer:v3")})
    assert [row["finding_id"] for row in result] == ["route", "weak"]
    gate = result[0]["ranking"]
    assert gate["status"] == "ready_to_test" and gate["priority_rank"] == 1
    assert gate["quality_prerequisites"] == {"required": [CHECK], "unmet": []}
    assert gate["source_inputs"][CHECK]["source_ref"] == "routing-study:answer:v3"


@pytest.mark.parametrize("value", ["failed", "regressed", True, 1])
def test_any_other_check_result_keeps_the_gate_closed(value):
    result = ranked([routing(), finding("other", confidence="low")], {CHECK: declared(value)})
    assert [row["finding_id"] for row in result] == ["other", "route"]
    assert "quality_check_not_passed:" + CHECK in result[1]["ranking"]["reasons"]
    assert result[1]["ranking"]["status"] == "needs_evidence_or_review"


def test_a_passed_check_must_carry_a_source_reference():
    result = ranked([routing()], {CHECK: {"value": "passed", "kind": "declared", "source_ref": ""}})
    assert "quality_check_required:" + CHECK in result[0]["ranking"]["reasons"]


def test_finding_level_declaration_opens_only_that_finding():
    values = [routing("a"), routing("b"), finding("other", confidence="low")]
    result = rank_findings(
        values,
        inputs={
            "parallelize_tools": STANDARD,
            "route_to_smaller_model": STANDARD,
            "b": {CHECK: declared("passed", "quality-report:abc123")},
        },
    )
    assert [row["finding_id"] for row in result] == ["b", "other", "a"]


def test_dimension_sorts_are_unchanged_but_priority_rank_records_the_gate():
    result = rank_findings(
        [routing(), finding("other", confidence="low")],
        inputs={"parallelize_tools": STANDARD, "route_to_smaller_model": STANDARD},
        sort_by="confidence",
    )
    assert [row["finding_id"] for row in result] == ["route", "other"]
    assert result[0]["ranking"]["priority_rank"] == 2


def test_rankings_stored_before_the_gate_still_sort():
    old = ranked([finding("a"), finding("b", confidence="low")])
    for row in old:
        del row["ranking"]["quality_prerequisites"]
    assert [row["finding_id"] for row in finalize_ranking(old)] == ["a", "b"]


def test_cluster_is_gated_when_any_member_lacks_a_passed_check():
    checked, unchecked = routing("a"), routing("b")
    checked_rank = ranked([checked], {CHECK: declared("passed")})[0]
    unchecked_rank = ranked([unchecked])[0]
    members = [
        {"run_id": "r1", "finding_id": "a", "finding": checked_rank},
        {"run_id": "r2", "finding_id": "b", "finding": unchecked_rank},
    ]
    result = cluster_ranking(members)
    assert result["quality_prerequisites"] == {"required": [CHECK], "unmet": [CHECK]}
    assert result["status"] == "needs_evidence_or_review"
    passed = cluster_ranking(members[:1])
    assert passed["quality_prerequisites"]["unmet"] == []


def _trace():
    trace = AgentTrace("gate")
    record_model_call(
        "plan",
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=400,
        model="large-model",
        input_tokens=300,
        output_tokens=60,
        token_provenance="provider",
        trace=trace,
    )
    return trace


def test_rule_requires_the_check_and_says_so_while_keeping_the_finding_id():
    rule = next(rule for rule in BUILTIN_RULES if rule.rule_id == "route_to_smaller_model")
    assert rule.version == "1.3" and CHECK in rule.ranking_requirements
    trace = _trace()
    result = build_diagnosis(trace)["findings"]
    route = next(row for row in result if row["type"] == "route_to_smaller_model")
    assert route["title"] == "Check quality before routing `plan` to a smaller model"
    assert route["rewrite"]["hint"].startswith("A quality check is required first")
    assert route["validation"]["acceptance_criteria"].startswith("an independent quality check")
    assert "quality_check_required:" + CHECK in route["ranking"]["reasons"]
    legacy = {
        "type": "route_to_smaller_model",
        "title": "Route `plan` to a cheaper model",
        "affected_spans": sorted(route["affected_spans"]),
    }
    digest = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    assert route["finding_id"] == "al_route_to_smaller_model_" + digest

    trace.metadata[RANKING_KEY] = {
        "route_to_smaller_model": {CHECK: declared("passed", "routing-study:plan:v1")}
    }
    reopened = next(
        row for row in build_diagnosis(trace)["findings"] if row["type"] == "route_to_smaller_model"
    )
    assert reopened["finding_id"] == route["finding_id"]
    assert reopened["ranking"]["quality_prerequisites"]["unmet"] == []
