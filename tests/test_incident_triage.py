from __future__ import annotations

import copy
import json

import pytest

from agentloop.graph import ExecutionGraph
from agentloop.semantic_waste import read_semantic_waste
from examples.incident_triage import FIXTURES, inspect_incident, main, run_incident
from examples.reference_support import digest


@pytest.mark.parametrize("fixture", FIXTURES, ids=[item["id"] for item in FIXTURES])
def test_baseline_and_hybrid_triage_frozen_signals_without_actions(fixture):
    for variant in ("baseline", "hybrid"):
        trace = run_incident(
            copy.deepcopy(fixture["input"]), variant, fixture["id"], digest(FIXTURES)
        )
        assert trace.metadata["output"] == fixture["expected"]
        assert trace.metadata["output"]["response"]["execute"] is False
        assert trace.metadata["output"]["disposition"]["execution_permitted"] is False
        assert {"anomaly", "severity", "response", "disposition"} <= {
            item.event_id for item in trace.events
        }
        assert ExecutionGraph.from_trace(trace).dependency_summary()["valid"]
        assert trace.metadata["action_mode"] == "inert_proposals_only"


def test_cheap_proxy_misses_incidents_and_over_escalates_noise():
    cases = {item["id"]: item for item in FIXTURES}
    noisy, severe = cases["noisy"], cases["critical-error"]
    noise = run_incident(noisy["input"], "cheap", noisy["id"], digest(FIXTURES)).metadata["output"]
    missed = run_incident(severe["input"], "cheap", severe["id"], digest(FIXTURES)).metadata[
        "output"
    ]
    assert noise["anomaly"] is True and noisy["expected"]["anomaly"] is False
    assert noise["disposition"]["escalate"] is True
    assert missed["anomaly"] is False and severe["expected"]["anomaly"] is True
    assert missed["disposition"]["escalate"] is False
    assert (
        noise["disposition"]["execution_permitted"]
        is missed["disposition"]["execution_permitted"]
        is False
    )


def test_ambiguous_evidence_abstains_and_failure_has_no_proposal():
    cases = {item["id"]: item for item in FIXTURES}
    case = cases["ambiguous"]
    unknown = run_incident(case["input"], "hybrid", case["id"], digest(FIXTURES))
    assert unknown.metadata["output"]["anomaly"] is None
    assert unknown.metadata["output"]["severity"] == "unknown"
    assert unknown.metadata["output"]["disposition"]["rollback_proposed"] is None
    failure = cases["critical-error"]
    failed = run_incident(failure["input"], "failing", failure["id"], digest(FIXTURES))
    assert failed.metadata["agentloop.workflow"]["status"] == "failed"
    assert "output" not in failed.metadata
    assert failed.events[-1].event_id == "response" and failed.events[-1].status == "error"


def test_semantic_findings_never_claim_remediation_safety_or_remove_required_checks():
    for case in (FIXTURES[0], next(item for item in FIXTURES if item["id"] == "critical-error")):
        trace = run_incident(case["input"], "baseline", case["id"], digest(FIXTURES))
        inspect_incident(trace, case["input"], "baseline")
        evidence = {
            item["definition"]["investigation_id"]: item
            for item in read_semantic_waste(trace)["cases"]
        }
        assert evidence["context-relevance"]["status"] == "supported"
        assert evidence["repeated-severity"]["status"] == (
            "retained" if case["input"]["verification"] else "supported"
        )
        for card in trace.report()["finding_candidates"]:
            if card["type"] in {"semantic_redundancy", "context_relevance"}:
                assert card["confidence"] == "low"
                assert card["estimated_latency_savings_ms"] is None


def test_reference_exports_all_incidents_and_incomplete_steps(tmp_path):
    original = copy.deepcopy(FIXTURES)
    report = main(tmp_path / "incidents")
    assert FIXTURES == original
    assert len(report["comparisons"]) == 3 * len(FIXTURES)
    assert any(item["candidate_quality"] is None for item in report["comparisons"])
    assert any(
        item["candidate_quality"] < 1
        for item in report["comparisons"]
        if item["variant"] == "cheap"
    )
    study = json.loads((tmp_path / "incidents" / "failing" / "study-report.json").read_text())
    assert study["conditions"]["candidate"]["run_count"] == len(FIXTURES)
    assert study["conditions"]["candidate"]["metrics"]["quality_score"]["missing_count"] == 1
    assert study["includes_synthetic_data"]
