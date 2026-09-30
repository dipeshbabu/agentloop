from __future__ import annotations

from copy import deepcopy
from itertools import permutations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, _quickstart_trace, app
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.ranking import RANKING_KEY, cluster_ranking, rank_findings
from agentloop.rules import (
    AnalysisContext,
    FindingCandidate,
    FindingRule,
    RecommendationType,
    run_rules,
)
from agentloop.server import app as server_app
from agentloop.store import SQLiteTraceStore


def finding(identity, *, latency=100, cost=0.1, confidence="high", evidence="declared"):
    return {
        "finding_id": identity,
        "type": "parallelize_tools",
        "title": "Investigate " + identity,
        "severity": "medium",
        "rule_id": "fixture",
        "rule_version": "1",
        "confidence": confidence,
        "evidence_level": evidence,
        "affected_spans": [identity],
        "evidence": [{"span_id": identity, "duration_ms": latency * 2}],
        "savings": {"estimated_latency_savings_ms": latency, "estimated_cost_savings_usd": cost},
        "estimate": {
            "estimator_id": "fixture",
            "estimator_version": "1",
            "calibrated": False,
            "inputs": {"cost_status": "complete"},
            "unmodeled_metrics": [],
        },
        "metadata": {},
        "rewrite": {"patchable": True},
        "validation": {},
    }


def input_value(value, *, kind="declared", source="review:v1"):
    return {"value": value, "kind": kind, "source_ref": source}


def inputs(**changes):
    return {
        "quality_risk": input_value("low"),
        "reversible": input_value(True),
        "validation_effort_minutes": input_value(10),
        "validation_cost_usd": input_value(0.1),
        **changes,
    }


def ranked(values, *, declared=None, sort_by="priority"):
    return rank_findings(
        values, inputs={"fixture": inputs() if declared is None else declared}, sort_by=sort_by
    )


def test_moderate_supported_opportunity_precedes_large_speculative_saving():
    moderate = finding("moderate", latency=20)
    speculative = finding("speculative", latency=100000, cost=1000, confidence="low")
    result = ranked([speculative, moderate])
    assert [row["finding_id"] for row in result] == ["moderate", "speculative"]
    assert result[0]["priority_score"] > 0 and result[1]["priority_score"] == 0
    assert result[1]["ranking"]["reasons"] == ["confidence_weak_or_unknown"]


def test_ranking_is_permutation_stable_and_does_not_mutate_predictions():
    values = [finding("a", latency=10), finding("b", latency=40), finding("c", latency=20)]
    saved = deepcopy(values)
    expected = ranked(values)
    for order in permutations(values):
        assert ranked(list(order)) == expected
    assert values == saved
    assert {row["finding_id"]: row["savings"] for row in expected} == {
        row["finding_id"]: row["savings"] for row in saved
    }
    assert expected[0]["ranking"]["components"]["latency"]["kind"] == "estimated"
    assert expected[0]["ranking"]["estimate_provenance"]["calibrated"] is False


@pytest.mark.parametrize(
    "missing", ["quality_risk", "reversible", "validation_effort_minutes", "validation_cost_usd"]
)
def test_unknown_inputs_never_receive_ready_priority(missing):
    declared = inputs()
    declared.pop(missing)
    value = ranked([finding("a")], declared=declared)[0]
    assert value["priority_score"] == 0
    assert "missing_input:" + missing in value["ranking"]["reasons"]


@pytest.mark.parametrize(
    "change",
    [
        "unknown_cost",
        "unmodeled_cost",
        "weak_evidence",
        "missing_span",
        "high_risk",
        "irreversible",
    ],
)
def test_missing_cost_and_weak_or_risky_evidence_are_explicit(change):
    value, declared = finding("a"), inputs()
    if change == "unknown_cost":
        value["savings"]["estimated_cost_savings_usd"] = None
    elif change == "unmodeled_cost":
        value["estimate"]["unmodeled_metrics"] = ["cost"]
    elif change == "weak_evidence":
        value["evidence_level"] = "inferred"
    elif change == "missing_span":
        value["evidence"] = []
    elif change == "high_risk":
        declared["quality_risk"] = input_value("high")
    else:
        declared["reversible"] = input_value(False)
    result = ranked([value], declared=declared)[0]
    assert result["priority_score"] == 0 and result["ranking"]["reasons"]
    assert not result["ranking"]["automatic_application_allowed"]


def test_dimension_sorts_keep_unknowns_last_without_changing_readiness():
    a, b = finding("a", cost=None), finding("b", cost=2, confidence="low")
    assert [row["finding_id"] for row in ranked([a, b], sort_by="cost")] == ["b", "a"]
    assert all(row["priority_score"] == 0 for row in ranked([a, b], sort_by="cost"))
    declared = {
        "a": inputs(validation_effort_minutes=input_value(20)),
        "b": inputs(validation_effort_minutes=input_value(1)),
    }
    assert [
        row["finding_id"]
        for row in rank_findings([a, b], inputs=declared, sort_by="validation_effort")
    ] == ["b", "a"]


def test_rule_authors_declare_generic_required_inputs():
    from agentloop.graph import ExecutionGraph

    def detect(context):
        return [
            FindingCandidate(RecommendationType.PARALLELIZE_TOOLS, "custom", "why", "hint", "high")
        ]

    rule = FindingRule("custom", "1", detect, ranking_requirements=("validation_dataset",))
    context = AnalysisContext({}, ExecutionGraph.from_trace(_quickstart_trace()))
    candidates, errors = run_rules(context, rules=(rule,))
    assert not errors and "validation_dataset" in candidates[0].to_dict()["ranking_requirements"]
    value = finding("a")
    value["ranking_requirements"] = rule.ranking_requirements
    assert "missing_input:validation_dataset" in ranked([value])[0]["ranking"]["reasons"]
    assert (
        ranked([value], declared=inputs(validation_dataset=input_value("dataset-v1")))[0][
            "priority_score"
        ]
        > 0
    )


def test_finding_annotations_override_individual_rule_inputs_without_losing_references():
    value = finding("a")
    result = rank_findings(
        [value],
        inputs={
            "fixture": inputs(),
            "a": {"quality_risk": input_value("medium", source="specific:v2")},
        },
    )[0]
    assert result["priority_score"] > 0
    assert result["ranking"]["components"]["quality_risk"]["source_ref"] == "specific:v2"
    assert result["ranking"]["components"]["validation_effort"]["source_ref"] == "review:v1"


def test_alternative_ranking_estimate_keeps_original_prediction():
    value = finding("a", cost=None)
    result = ranked(
        [value],
        declared=inputs(
            estimated_cost_savings_usd=input_value(0, kind="estimated", source="host-estimate:v1")
        ),
    )[0]
    assert result["savings"]["estimated_cost_savings_usd"] is None
    assert result["ranking"]["components"]["cost"] == {
        "value": 0,
        "kind": "estimated",
        "source_ref": "host-estimate:v1",
        "unit": "USD",
    }


def test_queue_reuses_compatible_selection_when_inputs_are_unchanged(tmp_path, monkeypatch):
    store = SQLiteTraceStore(str(tmp_path / "selection.db"))
    store.save_diagnosis({"run_id": "run", "findings": ranked([finding("a")])})
    monkeypatch.setattr(
        "agentloop.ranking.select_compatible", lambda values: pytest.fail("redundant selection")
    )
    assert store.optimization_queue()[0]["ranking"]["components"]["latency"]["value"] == 100


@pytest.mark.parametrize(
    "bad",
    [
        input_value("low", source=" "),
        input_value("low", kind="measured"),
        {"value": "low"},
        input_value(["low"]),
    ],
)
def test_malformed_input_is_not_treated_as_known(bad):
    result = ranked([finding("a")], declared=inputs(quality_risk=bad))[0]
    assert result["priority_score"] == 0
    assert "invalid_input:quality_risk" in result["ranking"]["reasons"]


def test_unrelated_payload_is_not_copied_into_ranking_inputs():
    value = ranked([finding("a")], declared={**inputs(), "raw_private_payload": "secret"})[0]
    assert "raw_private_payload" not in value["ranking"]["source_inputs"]


def test_queue_preserves_member_references_and_deduplicates_spans(tmp_path):
    store = SQLiteTraceStore(str(tmp_path / "ranking.db"))
    one, two = finding("a", latency=100), finding("b", latency=60)
    two.update(
        title=one["title"], affected_spans=one["affected_spans"], evidence=deepcopy(one["evidence"])
    )
    values = ranked([one, two])
    store.save_diagnosis({"run_id": "run", "findings": values})
    item = store.optimization_queue()[0]
    assert item["estimated_latency_savings_ms"] == 100
    assert item["ranking"]["components"]["latency"]["value"] == 100
    assert item["ranking"]["components"]["impact"]["value"] == 200
    assert item["priority_score"] > 0 and not item["safe_to_auto_patch"]
    assert len(item["ranking"]["members"]) == 2
    members = store.list_findings()
    assert cluster_ranking(members) == cluster_ranking(list(reversed(members)))


def test_queue_does_not_upgrade_a_filtered_invalid_annotation(tmp_path):
    value = ranked(
        [finding("a")], declared=inputs(estimated_cost_savings_usd=input_value(1, kind="observed"))
    )[0]
    store = SQLiteTraceStore(str(tmp_path / "invalid.db"))
    store.save_diagnosis({"run_id": "run", "findings": [value]})
    assert store.optimization_queue()[0]["priority_score"] == 0


def test_queue_namespaces_reused_run_ids_by_project(tmp_path):
    store = SQLiteTraceStore(str(tmp_path / "projects.db"))
    values = ranked([finding("a")])
    for project in ("one", "two"):
        store.save_diagnosis({"run_id": "same", "findings": values}, project_id=project)
    item = store.optimization_queue()[0]
    assert item["run_count"] == 2
    assert item["estimated_latency_savings_ms"] == 200
    assert item["ranking"]["components"]["latency"]["value"] == 200
    assert item["ranking"]["components"]["frequency"]["value"] == 2
    assert len(item["affected_run_refs"]) == 2


def test_numeric_overflow_remains_unavailable_instead_of_high_priority():
    value = finding("a")
    value["affected_spans"] = ["a", "b"]
    value["evidence"] = [{"span_id": name, "duration_ms": 1e308} for name in ("a", "b")]
    result = ranked([value])[0]
    assert result["priority_score"] == 0
    assert result["ranking"]["components"]["impact"]["value"] is None


def test_json_html_cli_and_http_keep_ranking_components(tmp_path, monkeypatch):
    import json

    from agentloop.server import store as store_dependency

    trace = _quickstart_trace()
    trace.metadata[RANKING_KEY] = {
        "parallelize_tools": inputs(
            quality_risk=input_value("medium", source="<script>reference</script>")
        )
    }
    diagnosis = build_diagnosis(trace, sort_by="latency")
    assert diagnosis["ranking"]["sort_by"] == "latency"
    path, output = tmp_path / "trace.json", tmp_path / "analysis.json"
    trace.export_json(path)
    response = CliRunner().invoke(
        app, ["analyze", str(path), "--sort-by", "latency", "--json-out", str(output)]
    )
    assert response.exit_code == 0, response.output
    assert json.loads(output.read_text())["diagnosis"] == diagnosis
    html = analysis_to_html(_analysis_payload(trace, sort_by="latency"))
    assert "Ranking components" in html and "Test readiness" in html
    assert "&lt;script&gt;reference&lt;/script&gt;" in html
    assert "<script>" not in html
    database = SQLiteTraceStore(str(tmp_path / "api.db"))
    database.save_trace(trace)
    server_app.dependency_overrides[store_dependency] = lambda: database
    try:
        client = TestClient(server_app)
        result = client.get(f"/v1/traces/{trace.run_id}/diagnosis?sort_by=latency")
        assert result.status_code == 200 and result.json() == diagnosis
        queue = client.get("/v1/optimization-queue?sort_by=confidence")
        assert queue.status_code == 200 and queue.json()["sort_by"] == "confidence"
        assert all("components" in row["ranking"] for row in queue.json()["queue"])
        assert client.get(f"/v1/traces/{trace.run_id}/diagnosis?sort_by=invalid").status_code == 422
    finally:
        server_app.dependency_overrides.pop(store_dependency, None)
