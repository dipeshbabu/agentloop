from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

import pytest
from test_structured_quality import case
from workflow_fixtures import pipeline

from agentloop import attach_quality_report
from agentloop.quality import QualityValidationError, build_quality_report
from agentloop.replay import build_replay_report, replay_report_to_markdown
from agentloop.structured_quality import EVIDENCE_KEY, read_quality_evidence


def test_replay_uses_attached_evidence_without_a_separate_report():
    before, after = pipeline(), pipeline(run_id="candidate")
    fixture = case("decision", "x", "x", labels=["x"])
    fixture.pop("candidate_output")
    quality = build_quality_report([fixture], baseline_trace=before, candidate_trace=after)
    attach_quality_report(before, quality, side="baseline")
    attach_quality_report(after, quality)
    replay = build_replay_report(before, after)
    assert not replay["gates"]["passed"]
    assert replay["deltas"]["quality_score_delta"] is None
    assert any(
        item["name"] == "quality_evidence" and item["indeterminate"]
        for item in replay["gates"]["results"]
    )
    assert "unavailable" in replay_report_to_markdown(replay)
    explicit = build_replay_report(before, after, quality_report=quality)
    assert "Candidate score: unavailable" in replay_report_to_markdown(explicit)


def test_quality_reports_cannot_be_rebound_to_different_traces():
    before, after = pipeline(), pipeline(run_id="candidate")
    report = build_quality_report(
        [case("decision", "x", "x", labels=["x"])], baseline_trace=before, candidate_trace=after
    )
    with pytest.raises(QualityValidationError, match="different traces"):
        build_replay_report(before, pipeline(run_id="wrong"), quality_report=report)
    changed = copy.deepcopy(report)
    changed["candidate_score"] = 0
    with pytest.raises(QualityValidationError, match="changed"):
        attach_quality_report(after, changed)


def test_large_label_domains_use_sparse_confusion_storage():
    labels = [f"label-{index}" for index in range(2000)]
    report = build_quality_report([case("decision", labels[0], labels[-1], labels=labels)])
    group = next(iter(report["classification"]["candidate"].values()))
    assert group["matrix"] == [[0, 1999, 1]]
    assert len(group["per_label"]) == 2000


def test_missing_output_and_reference_counts_are_both_retained():
    fixture = case("decision", "x", "x", labels=["x"])
    fixture.pop("expected")
    fixture.pop("candidate_output")
    group = next(iter(build_quality_report([fixture])["classification"]["candidate"].values()))
    assert group["unlabelled_count"] == group["missing_output_count"] == 1
    assert group["accuracy_on_available_outputs"] is None


def test_quality_evidence_survives_native_otlp_roundtrips():
    from agentloop.otel import trace_from_otel, trace_to_otel

    trace = pipeline()
    report = build_quality_report([case("decision", "x", "x", labels=["x"])], candidate_trace=trace)
    attach_quality_report(trace, report)
    original = copy.deepcopy(trace.metadata[EVIDENCE_KEY])
    for _ in range(2):
        trace = trace_from_otel(trace_to_otel(trace))
    assert trace.metadata[EVIDENCE_KEY] == original
    assert read_quality_evidence(trace)["passed"]


def test_studies_keep_unknown_quality_unmatched_examples_and_decision_counts(tmp_path):
    from agentloop.studies import summarize_study

    for task in ("a", "b", "unmatched"):
        before = pipeline(run_id=f"before-{task}", task=task)
        after = pipeline(run_id=f"after-{task}", task=task)
        fixture = case("decision", "x", "x", labels=["x"], identity=task)
        if task != "a":
            fixture.pop("candidate_output")
        quality = build_quality_report([fixture], baseline_trace=before, candidate_trace=after)
        attach_quality_report(before, quality, side="baseline")
        before.export_json(tmp_path / "before" / f"{task}.json")
        if task != "unmatched":
            after.metadata["quality_score"] = 1
            attach_quality_report(after, quality)
            after.export_json(tmp_path / "after" / f"{task}.json")
    manifest = {
        "schema_version": "1.0",
        "name": "structured decisions",
        "baseline": "before",
        "conditions": {"before": ["before/*.json"], "after": ["after/*.json"]},
        "pairing_keys": ["task_id", "seed"],
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = summarize_study(path)
    assert report["conditions"]["after"]["metrics"]["quality_score"]["missing_count"] == 1
    assert report["conditions"]["after"]["metrics"]["success"]["missing_count"] == 1
    assert report["comparisons"]["after"]["pair_count"] == 2
    assert len(report["comparisons"]["after"]["unmatched"]) == 1
    assert report["comparisons"]["after"]["metrics"]["decision_count"]["count"] == 2
    assert (
        report["conditions"]["after"]["runs"][0]["quality_evidence"]["cases"][0]["scorer"][
            "version"
        ]
        == "1.0"
    )


def test_ledger_reuses_structured_quality_and_remains_idempotent(tmp_path):
    from agentloop import AgentTrace, WorkflowInfo, workflow_metadata
    from agentloop.entrypoint import _quickstart_trace
    from agentloop.findings import build_diagnosis
    from agentloop.intervention_service import create_stored_intervention
    from agentloop.store import SQLiteTraceStore

    before = _quickstart_trace()
    before.metadata.update(
        workflow_metadata(
            WorkflowInfo("routing", "1"), status="completed", metadata={"synthetic": True}
        )
    )
    after = AgentTrace.from_dict(before.to_dict())
    after.run_id = "structured-candidate"
    for event in after.events:
        event.run_id = after.run_id
    database = SQLiteTraceStore(str(tmp_path / "ledger.db"))
    database.save_trace(before)
    database.save_trace(after)
    finding = build_diagnosis(before)["findings"][0]
    fixtures = [
        case(
            "decision",
            "billing",
            {"route": "billing"},
            labels=["billing", "other"],
            field="route",
            before={"route": "other"},
        )
    ]
    request = {
        "baseline_run_id": before.run_id,
        "candidate_run_id": after.run_id,
        "target_finding_ids": [finding["finding_id"]],
        "intervention_type": "structured-route-fixture",
        "quality_fixtures": fixtures,
    }
    first = create_stored_intervention(database, request)
    assert create_stored_intervention(database, request) == first
    assert first["measured"]["quality"]["schema_version"] == "2.0"
    assert first["measured"]["quality"]["candidate_score"] == 1
    assert first["predicted"]["findings"][0] == finding


def test_public_structured_workflow_example_retains_all_outcomes(tmp_path):
    example = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "structured_quality_workflow.py")
    )
    report = example["run"](tmp_path)
    initial = (tmp_path / "study-results.json").read_bytes()
    assert example["run"](tmp_path) == report
    assert (tmp_path / "study-results.json").read_bytes() == initial
    assert report["comparisons"]["candidate"]["pair_count"] == 4
    assert len(report["comparisons"]["candidate"]["unmatched"]) == 1
    assert report["conditions"]["candidate"]["metrics"]["quality_score"]["missing_count"] == 1


def test_http_supports_builtins_but_never_imports_custom_scorers(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentloop.server import app

    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "quality.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    client = TestClient(app)
    response = client.post(
        "/v1/quality-reports", json={"fixtures": [case("decision", "x", "x", labels=["x"])]}
    )
    assert response.status_code == 200 and response.json()["schema_version"] == "2.0"
    custom = case("custom", True, True, callable="does_not_exist:danger", version="1")
    assert client.post("/v1/quality-reports", json={"fixtures": [custom]}).status_code == 400
    builtin = case("decision", "x", "x", labels=["x"], callable="does_not_exist:danger")
    assert client.post("/v1/quality-reports", json={"fixtures": [builtin]}).status_code == 422
