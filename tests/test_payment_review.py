from __future__ import annotations

import copy
import json

import pytest

from agentloop.graph import ExecutionGraph
from examples.payment_review import FIXTURES, main, run_payment
from examples.reference_support import digest


@pytest.mark.parametrize("fixture", FIXTURES, ids=[item["id"] for item in FIXTURES])
def test_baseline_and_hybrid_match_independent_fixture_policy(fixture):
    for variant in ("baseline", "hybrid"):
        trace = run_payment(
            copy.deepcopy(fixture["input"]), variant, fixture["id"], digest(FIXTURES)
        )
        assert trace.metadata["output"] == fixture["expected"]
        assert {item.event_id for item in trace.events} == {
            "suspicious",
            "eligibility",
            "manual_review",
            "route",
        }
        assert ExecutionGraph.from_trace(trace).dependency_summary()["valid"]
        assert trace.metadata["policy_basis"] == "fictional_evaluation_fixture"
        assert trace.report()["execution"]["workflow_id"] == "reference.payment-review"


def test_cheaper_proxy_exposes_false_positives_and_false_negatives():
    by_id = {item["id"]: item for item in FIXTURES}
    high, low = by_id["high-value-legitimate"], by_id["small-duplicate"]
    positive = run_payment(high["input"], "cheap", high["id"], digest(FIXTURES))
    negative = run_payment(low["input"], "cheap", low["id"], digest(FIXTURES))
    assert (
        positive.metadata["output"]["suspicious"] is True
        and high["expected"]["suspicious"] is False
    )
    assert (
        negative.metadata["output"]["suspicious"] is False and low["expected"]["suspicious"] is True
    )
    assert positive.report()["model_call_count"] == negative.report()["model_call_count"] == 0


def test_ambiguity_is_an_explicit_manual_review_outcome_not_false_certainty():
    fixture = next(item for item in FIXTURES if item["id"] == "missing-evidence")
    trace = run_payment(fixture["input"], "hybrid", fixture["id"], digest(FIXTURES))
    assert trace.metadata["output"] == {
        "suspicious": None,
        "eligible": None,
        "manual_review": True,
        "route": "manual_review",
    }
    assert trace.metadata["agentloop.workflow"]["status"] == "completed"
    # Correctly returning a defer label is different from an execution failure.
    assert all(item.status == "ok" for item in trace.events)


def test_failed_step_does_not_fabricate_refund_output_or_bill():
    fixture = FIXTURES[0]
    trace = run_payment(fixture["input"], "failing", fixture["id"], digest(FIXTURES))
    assert "output" not in trace.metadata
    assert trace.metadata["agentloop.workflow"]["status"] == "failed"
    assert trace.events[-1].event_id == "eligibility"
    assert trace.events[-1].status == "error"
    assert "provider_reported_cost_usd" not in trace.events[-1].metadata


def test_fixture_schema_contains_no_payment_or_person_identifiers():
    allowed = {
        "amount_band",
        "duplicate_signal",
        "evidence_complete",
        "within_fixture_window",
        "proof_present",
        "conflicting_evidence",
        "required_review",
    }
    assert all(set(item["input"]) == allowed for item in FIXTURES)
    assert all(item["input"]["amount_band"] in {"low", "normal", "high"} for item in FIXTURES)


def test_payment_evidence_retains_all_cases_and_does_not_equate_cost_with_safety(tmp_path):
    original = copy.deepcopy(FIXTURES)
    report = main(tmp_path / "payments")
    assert FIXTURES == original
    assert len(report["comparisons"]) == len(FIXTURES) * 3
    cheap = [item for item in report["comparisons"] if item["variant"] == "cheap"]
    assert all(item["candidate_fixture_cost_usd"] == 0 for item in cheap)
    assert any(item["candidate_quality"] < 1 and not item["passed"] for item in cheap)
    assert len([item for item in report["comparisons"] if item["candidate_quality"] is None]) == 1
    study = json.loads((tmp_path / "payments" / "failing" / "study-report.json").read_text())
    assert study["conditions"]["candidate"]["run_count"] == len(FIXTURES)
    assert study["conditions"]["candidate"]["metrics"]["quality_score"]["missing_count"] == 1
    assert study["includes_synthetic_data"]
