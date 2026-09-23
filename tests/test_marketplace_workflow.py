from __future__ import annotations

import copy
import json

import pytest

from agentloop.graph import ExecutionGraph
from agentloop.semantic_waste import read_semantic_waste
from examples.marketplace_workflow import FIXTURES, inspect_listing, main, run_listing
from examples.reference_support import digest


@pytest.mark.parametrize("fixture", FIXTURES, ids=[item["id"] for item in FIXTURES])
def test_baseline_and_hybrid_match_independent_listing_labels(fixture):
    for variant in ("baseline", "hybrid"):
        trace = run_listing(
            copy.deepcopy(fixture["input"]), variant, fixture["id"], digest(FIXTURES)
        )
        assert trace.metadata["output"] == fixture["expected"]
        assert {"duplicate", "moderation", "category", "route"} <= {
            item.event_id for item in trace.events
        }
        assert ExecutionGraph.from_trace(trace).dependency_summary()["valid"]
        assert trace.metadata["action_mode"] == "review_labels_only"


def test_near_duplicate_is_not_an_exact_match_and_keyword_is_not_policy():
    cases = {item["id"]: item for item in FIXTURES}
    for identity, key in (
        ("near-variant", "duplicate"),
        ("benign-keyword", "moderation"),
        ("policy-flag", "moderation"),
    ):
        case = cases[identity]
        trace = run_listing(case["input"], "cheap", identity, digest(FIXTURES))
        assert trace.metadata["output"][key] != case["expected"][key]
        assert trace.metadata["output"]["route"] != case["expected"]["route"]
        assert trace.report()["model_call_count"] == 0


def test_ambiguous_listing_is_deferred_without_inventing_a_category():
    for case in (
        item for item in FIXTURES if item["id"] in {"category-ambiguity", "missing-evidence"}
    ):
        trace = run_listing(case["input"], "hybrid", case["id"], digest(FIXTURES))
        assert trace.metadata["output"]["category"] is None
        assert trace.metadata["output"]["route"] == "manual_review"
        assert trace.metadata["agentloop.workflow"]["status"] == "completed"


def test_overlapping_work_investigation_respects_required_verification():
    for case in (
        FIXTURES[0],
        next(item for item in FIXTURES if item["id"] == "required-verification"),
    ):
        trace = run_listing(case["input"], "baseline", case["id"], digest(FIXTURES))
        inspect_listing(trace, case["input"], "baseline")
        result = read_semantic_waste(trace)["cases"][0]
        assert result["status"] == ("retained" if case["input"]["verification"] else "supported")
        assert result["confidence"] != "high"


def test_per_field_quality_keeps_stage_errors_and_final_routing_errors(tmp_path):
    original = copy.deepcopy(FIXTURES)
    report = main(tmp_path / "marketplace")
    assert FIXTURES == original
    assert len(report["comparisons"]) == 24
    index = next(index for index, item in enumerate(FIXTURES) if item["id"] == "near-variant")
    path = tmp_path / "marketplace" / "cheap" / f"case-{index:04d}"
    diagnostic = json.loads((path / "field-quality.json").read_text())
    fields = {
        item["case_id"].split(":", 1)[1]: item["candidate"]["score"] for item in diagnostic["cases"]
    }
    assert fields["duplicate"] == fields["route"] == 0
    assert fields["moderation"] == fields["category"] == fields["verified"] == 1
    full = json.loads((path / "quality.json").read_text())
    assert full["case_count"] == 1
    assert diagnostic["case_count"] == len(FIXTURES[index]["expected"])
    assert (path / "field-quality.md").exists()
    assert any(
        item["candidate_quality"] is None
        for item in report["comparisons"]
        if item["variant"] == "failing"
    )
    failure = json.loads(
        (tmp_path / "marketplace" / "analysis" / "failing-0000" / "trace.json").read_text()
    )
    assert (
        next(item for item in failure["events"] if item["event_id"] == "category")["status"]
        == "error"
    )
    study = json.loads((tmp_path / "marketplace" / "failing" / "study-report.json").read_text())
    assert study["conditions"]["candidate"]["metrics"]["quality_score"]["missing_count"] == 1
