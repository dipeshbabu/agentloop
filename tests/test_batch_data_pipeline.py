from __future__ import annotations

import copy
import json

import pytest

from agentloop.graph import ExecutionGraph
from examples.batch_data_pipeline import (
    aggregate_stages,
    dataset,
    main,
    quality_for_chunk,
    run_chunk,
)
from examples.reference_support import digest


def test_frozen_dataset_labels_are_independent_of_candidate_lookup_tables(monkeypatch):
    import examples.batch_data_pipeline as module

    original = dataset(12)
    monkeypatch.setitem(module.CATEGORIES, "widget", "wrong")
    monkeypatch.setitem(module.ENTITIES, "alpha", "wrong")
    assert dataset(12) == original


def test_batch_pipeline_profiles_many_records_as_one_workflow():
    rows = dataset(16)
    before, baseline = run_chunk(rows, "baseline", "chunk-1", digest(rows), capture_prompts=True)
    after, candidate = run_chunk(rows, "optimized", "chunk-1", digest(rows))
    assert before.report()["execution"]["workflow_id"] == "reference.batch-data"
    assert before.report()["model_call_count"] == 48
    assert after.report()["model_call_count"] == 1
    assert len(before.events) == 64
    assert len({event.event_id for event in before.events}) == len(before.events)
    assert ExecutionGraph.from_trace(before).dependency_summary()["valid"]
    assert ExecutionGraph.from_trace(after).dependency_summary()["valid"]
    assert baseline == candidate == {row["id"]: row["expected"] for row in rows}
    report = quality_for_chunk(rows, before, after, baseline, candidate, digest(rows))
    assert report["candidate_score"] == 1 and report["case_count"] == 3 * len(rows)
    assert not any(item["type"] == "runaway_loop" for item in before.report()["finding_candidates"])


def test_capture_opt_in_exposes_repeated_context_and_batch_evidence():
    rows = dataset(8)
    captured, _ = run_chunk(rows, "baseline", "chunk", digest(rows), capture_prompts=True)
    private, _ = run_chunk(rows, "baseline", "chunk", digest(rows))
    assert all(event.input_text is None for event in private.events)
    kinds = {item["type"] for item in captured.report()["finding_candidates"]}
    assert {"cache_context", "batch_model_calls"} <= kinds
    assert "cache_context" not in {item["type"] for item in private.report()["finding_candidates"]}
    for event in captured.events:
        if event.event_type == "model_call":
            assert event.metadata["batch_safe"] and event.metadata["side_effects"] == "none"
            assert event.metadata["schema_ref"].startswith("fixture:")


def test_failed_match_is_unknown_without_discarding_completed_fields():
    rows = dataset(8)
    before, baseline = run_chunk(rows, "baseline", "chunk", digest(rows))
    after, candidate = run_chunk(rows, "failing", "chunk", digest(rows))
    report = quality_for_chunk(rows, before, after, baseline, candidate, digest(rows))
    cases = {item["case_id"]: item for item in report["cases"]}
    assert cases["record-00003:fields"]["candidate"]["score"] == 1
    assert cases["record-00003:category"]["candidate"]["score"] == 1
    assert cases["record-00003:matches"]["candidate"]["score"] is None
    assert report["candidate_score"] is None
    assert len(after.metadata["record_outcomes"]) == len(rows)
    assert sum(item["status"] == "failed" for item in after.metadata["record_outcomes"]) == 1
    assert after.report()["cost_status"] == "partial"


def test_unmatched_entities_are_known_empty_pairs_not_failed_outputs():
    rows = dataset(8)
    before, baseline = run_chunk(rows, "baseline", "chunk", digest(rows))
    after, candidate = run_chunk(rows, "optimized", "chunk", digest(rows))
    report = quality_for_chunk(rows, before, after, baseline, candidate, digest(rows))
    empty = next(item for item in report["cases"] if item["case_id"] == "record-00002:matches")
    assert candidate["record-00002"]["matches"] == []
    assert empty["candidate"]["score"] == 1


def test_stage_aggregation_retains_distributions_coverage_and_cost_provenance():
    rows = dataset(8)
    trace, _ = run_chunk(rows, "failing", "chunk", digest(rows))
    stages = aggregate_stages([trace])
    assert stages["extract"]["call_count"] == 8
    assert stages["extract"]["latency_ms"]["count"] == 8
    assert stages["extract"]["latency_ms"]["p95"] is not None
    assert stages["match"]["failure_count"] == 1
    assert stages["match"]["failure_rate"] == 1 / 8
    assert stages["match"]["model_cost_usd"]["missing_count"] == 1
    assert stages["match"]["cost_complete"] is False
    assert stages["match"]["cost_scope"] == "synthetic_backend_reports_only"


def test_complete_reference_exports_chunk_studies_and_stage_substitution(tmp_path):
    summary = main(
        tmp_path / "data", records=12, chunk_size=4, capture_prompts=True, html_samples=1
    )
    assert summary["record_count"] == 12 and summary["chunk_count"] == 3
    assert summary["trace_volume"]["trace_count"] == 12
    assert summary["trace_volume"]["captured_prompt_bytes"] > 0
    assert len(list((tmp_path / "data" / "analysis").rglob("analysis.html"))) == 4
    assert len(summary["record_failures"]["failing"]) == 1
    study = json.loads((tmp_path / "data" / "optimized" / "study-report.json").read_text())
    assert study["comparisons"]["candidate"]["pair_count"] == 3
    substitution = json.loads((tmp_path / "data" / "substitution" / "comparison.json").read_text())
    cases = {item["condition"]: item for item in substitution["comparisons"]}
    assert cases["kind-rule"]["quality_preserved_on_examples"] is True
    assert cases["cheap-guess"]["quality_preserved_on_examples"] is False
    assert substitution["synthetic"] and substitution["sample_status"] == "exploratory"


@pytest.mark.parametrize("value", [0, 3, 10001, True])
def test_record_count_bounds_are_explicit(value):
    with pytest.raises(ValueError):
        dataset(value)


def test_reference_does_not_mutate_input_records():
    rows = dataset(8)
    original = copy.deepcopy(rows)
    for variant in ("baseline", "optimized", "cheap", "failing"):
        run_chunk(rows, variant, "chunk", digest(rows))
    assert rows == original
