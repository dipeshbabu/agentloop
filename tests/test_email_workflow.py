from __future__ import annotations

import copy
import json

import pytest

from agentloop import JudgeIdentity, LocalCallbackJudge
from agentloop.graph import ExecutionGraph
from agentloop.semantic_waste import read_semantic_waste
from examples.email_workflow import FIXTURES, inspect_email, main, run_email
from examples.reference_support import digest


@pytest.mark.parametrize("fixture", FIXTURES, ids=[item["id"] for item in FIXTURES])
def test_baseline_and_hybrid_execute_all_decision_stages_against_frozen_labels(fixture):
    for variant in ("baseline", "hybrid"):
        trace = run_email(copy.deepcopy(fixture["input"]), variant, fixture["id"], digest(FIXTURES))
        assert trace.metadata["output"] == fixture["expected"]
        ids = {event.event_id for event in trace.events}
        assert {"spam", "category", "priority", "route"} <= ids
        assert trace.metadata["agentloop.workflow"]["status"] == "completed"
        assert trace.report()["execution"]["workflow_id"] == "reference.email-pipeline"
        assert ExecutionGraph.from_trace(trace).dependency_summary()["valid"]
        assert not any(
            item["type"] == "runaway_loop" for item in trace.report()["finding_candidates"]
        )


def test_cheaper_rule_is_evaluated_not_assumed_correct():
    fixture = next(item for item in FIXTURES if item["id"] == "newsletter")
    trace = run_email(fixture["input"], "cheap", fixture["id"], digest(FIXTURES))
    assert trace.metadata["output"]["priority"] == "high"
    assert fixture["expected"]["priority"] == "low"
    assert trace.report()["model_call_count"] == 0


def test_fixture_cost_and_token_provenance_are_explicit():
    fixture = FIXTURES[0]
    trace = run_email(fixture["input"], "baseline", fixture["id"], digest(FIXTURES))
    assert trace.metadata["synthetic"] is True
    for event in trace.events:
        if event.event_type == "model_call":
            assert event.metadata["provider"] == "agentloop-fixture"
            assert event.metadata["cost_provenance"]["basis"] == "reported_by_synthetic_backend"
            assert event.metadata["tokenizer"] == "json-whitespace-fixture-v1"
            assert event.token_provenance == "tokenizer"
            assert event.input_text is None and event.output_text is None
    assert trace.report()["token_status"] == "exact"


def test_failure_remains_visible_without_an_output_or_invented_billing():
    fixture = next(item for item in FIXTURES if item["id"] == "billing")
    trace = run_email(fixture["input"], "failing", fixture["id"], digest(FIXTURES))
    assert trace.metadata["agentloop.workflow"]["status"] == "failed"
    assert "output" not in trace.metadata
    failure = next(item for item in trace.events if item.status == "error")
    assert failure.event_id == "category"
    assert "provider_reported_cost_usd" not in failure.metadata
    assert trace.report()["cost_status"] == "partial"


def test_semantic_fixture_retains_required_verification_and_supports_context_investigation():
    for fixture in (FIXTURES[0], next(item for item in FIXTURES if item["id"] == "verification")):
        trace = run_email(fixture["input"], "baseline", fixture["id"], digest(FIXTURES))
        inspect_email(trace, fixture["input"], "baseline")
        cases = {
            item["definition"]["investigation_id"]: item
            for item in read_semantic_waste(trace)["cases"]
        }
        assert cases["context-relevance"]["status"] == "supported"
        assert cases["priority-repetition"]["status"] == (
            "retained" if fixture["input"]["verification"] else "supported"
        )
        assert all(item["confidence"] != "high" for item in cases.values())


def test_custom_judge_failure_is_unknown_and_cannot_turn_into_semantic_findings():
    fixture = FIXTURES[0]
    trace = run_email(fixture["input"], "baseline", fixture["id"], digest(FIXTURES))

    def fail(*args, **kwargs):
        raise TimeoutError("private")

    judge = LocalCallbackJudge(JudgeIdentity.configured("fixture.failure", "1", {}), fail)
    inspect_email(trace, fixture["input"], "baseline", judge=judge)
    assert all(item["status"] == "unknown" for item in read_semantic_waste(trace)["cases"])
    assert not any(
        item["type"] in {"semantic_redundancy", "context_relevance"}
        for item in trace.report()["finding_candidates"]
    )


def test_full_example_exports_real_replays_studies_failures_and_semantic_evidence(tmp_path):
    original = copy.deepcopy(FIXTURES)
    report = main(tmp_path / "email")
    assert report["fixture_hash"] == digest(original)
    assert FIXTURES == original
    hybrid = [item for item in report["comparisons"] if item["variant"] == "hybrid"]
    assert all(item["candidate_quality"] == 1 for item in hybrid)
    assert all(
        item["candidate_fixture_cost_usd"] < item["baseline_fixture_cost_usd"] for item in hybrid
    )
    assert any(
        item["candidate_quality"] < 1
        for item in report["comparisons"]
        if item["variant"] == "cheap"
    )
    failed = [item for item in report["comparisons"] if item["candidate_quality"] is None]
    assert len(failed) == 1 and failed[0]["task_id"] == "billing"
    study = json.loads((tmp_path / "email" / "hybrid" / "study-report.json").read_text())
    assert study["comparisons"]["candidate"]["pair_count"] == len(FIXTURES)
    assert study["includes_synthetic_data"]
    assert (tmp_path / "email" / "analysis" / "baseline-0000" / "analysis.html").exists()
    with pytest.raises(FileExistsError):
        main(tmp_path / "email")
