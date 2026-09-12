from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentloop.cli import app as cli
from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis
from agentloop.intervention_service import create_stored_intervention, parse_replay_gates
from agentloop.interventions import (
    InterventionRecord,
    InterventionReferenceError,
    InterventionValidationError,
    build_intervention,
)
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates
from agentloop.server import app
from agentloop.store import SQLiteTraceStore


def pair():
    baseline = _quickstart_trace()
    candidate = copy.deepcopy(baseline)
    candidate.run_id = "candidate-run"
    for event in candidate.events:
        event.run_id = candidate.run_id
    diagnosis = build_diagnosis(baseline)
    targets = [finding["finding_id"] for finding in diagnosis["findings"]]
    return baseline, candidate, diagnosis, targets


def request_for(baseline, candidate, targets):
    return {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "target_finding_ids": targets,
        "intervention_type": "context_compression",
    }


def test_identity_is_order_independent_and_evidence_is_an_independent_snapshot():
    baseline, candidate, diagnosis, targets = pair()
    first = build_intervention(
        baseline,
        candidate,
        target_finding_ids=targets,
        intervention_type="compression",
        configuration={"b": 2, "a": 1},
        diagnosis=diagnosis,
    )
    second = build_intervention(
        baseline,
        candidate,
        target_finding_ids=list(reversed(targets)) + targets,
        intervention_type="compression",
        configuration={"a": 1, "b": 2},
        diagnosis=diagnosis,
    )
    assert first == second
    exported = first.to_dict()
    assert exported["predicted"]["findings"][0]["estimate"]["calibrated"] is False
    diagnosis["findings"][0]["savings"]["estimated_latency_savings_ms"] = 9999
    exported["metadata"]["changed"] = True
    assert first == InterventionRecord.from_dict(second.to_dict())
    assert "changed" not in first.to_dict()["metadata"]
    assert first.to_dict()["measured"]["deltas"]["runtime_ms_delta"] == 0


def test_failed_quality_and_unknown_cost_remain_observed_evidence():
    baseline, candidate, _, targets = pair()
    for trace in (baseline, candidate):
        for event in trace.events:
            if event.event_type == "model_call":
                event.model = "unpriced-model"
    quality = build_quality_report(
        [{"expected": "ok", "baseline_output": "ok", "candidate_output": "bad"}]
    )
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=targets,
        intervention_type="compression",
        quality_report=quality,
        gates=ReplayGates(min_quality_score=0.9),
    ).to_dict()
    assert record["gates_passed"] is False
    assert record["measured"]["quality"]["passed"] is False
    assert record["measured"]["deltas"]["cost_usd_delta"] is None
    assert record["measured"]["gates"]["cost_evaluable"] is False


def test_missing_targets_and_forged_identity_are_rejected():
    baseline, candidate, _, targets = pair()
    with pytest.raises(InterventionReferenceError):
        build_intervention(
            baseline, candidate, target_finding_ids=["missing"], intervention_type="change"
        )
    record = build_intervention(
        baseline, candidate, target_finding_ids=targets, intervention_type="change"
    ).to_dict()
    record["intervention_id"] = "int_forged"
    with pytest.raises(InterventionValidationError, match="identity"):
        InterventionRecord.from_dict(record)
    with pytest.raises(InterventionValidationError, match="distinct"):
        build_intervention(
            baseline, baseline, target_finding_ids=targets, intervention_type="change"
        )


@pytest.mark.parametrize(
    "gates",
    [
        {"unknown": 1},
        {"min_latency_improvement_pct": True},
        {"min_cost_improvement_pct": float("nan")},
        {"min_quality_score": 1.1},
        {"require_schema_valid": "false"},
    ],
)
def test_invalid_gate_configuration_is_rejected(gates):
    with pytest.raises(InterventionValidationError):
        parse_replay_gates(gates)


def test_stored_creation_uses_saved_prediction_snapshots(tmp_path):
    baseline, candidate, diagnosis, targets = pair()
    db = SQLiteTraceStore(str(tmp_path / "evidence.db"))
    db.save_trace(baseline)
    db.save_trace(candidate)
    diagnosis["findings"][0]["estimate"]["estimator_version"] = "historical-version"
    db.save_diagnosis(diagnosis)
    record = create_stored_intervention(db, request_for(baseline, candidate, targets))
    assert any(
        finding["estimate"]["estimator_version"] == "historical-version"
        for finding in record["predicted"]["findings"]
    )


def test_api_creates_retrieves_and_isolates_interventions(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    db = SQLiteTraceStore(str(tmp_path / "api.db"))
    key_a = db.create_api_key("a", "a")["api_key"]
    key_b = db.create_api_key("b", "b")["api_key"]
    baseline, candidate, _, targets = pair()
    db.save_trace(baseline, project_id="a")
    db.save_trace(candidate, project_id="a")
    client = TestClient(app)
    headers = {"X-AgentLoop-Key": key_a}
    payload = request_for(baseline, candidate, targets)
    response = client.post("/interventions", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    record = response.json()
    assert client.post("/interventions", json=payload, headers=headers).json() == record
    route = "/interventions/" + record["intervention_id"]
    assert client.get(route, headers=headers).json() == record
    assert client.get(route, headers={"X-AgentLoop-Key": key_b}).status_code == 404
    assert (
        client.post("/interventions", json=payload, headers={"X-AgentLoop-Key": key_b}).status_code
        == 404
    )
    assert client.get(route).status_code == 401
    payload["metadata"] = {"changed": True}
    assert client.post("/interventions", json=payload, headers=headers).status_code == 409
    payload["gates"] = {"require_schema_valid": "false"}
    assert client.post("/interventions", json=payload, headers=headers).status_code == 422


def test_http_rejects_executable_scorers_before_import(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    db = SQLiteTraceStore(str(tmp_path / "custom.db"))
    baseline, candidate, _, targets = pair()
    db.save_trace(baseline)
    db.save_trace(candidate)
    payload = request_for(baseline, candidate, targets)
    payload["quality_fixtures"] = [
        {
            "candidate_output": "x",
            "scorer": {"type": "custom", "callable": "nonexistent_module:execute"},
        }
    ]
    response = TestClient(app).post("/interventions", json=payload)
    assert response.status_code == 422
    assert "not accepted" in response.text


def test_cli_create_get_and_replay_export_failed_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cli.db"))
    baseline, candidate, _, targets = pair()
    db = SQLiteTraceStore(str(tmp_path / "cli.db"))
    db.save_trace(baseline)
    db.save_trace(candidate)
    request_path, exported, fetched = [
        tmp_path / name for name in ("request.json", "record.json", "fetched.json")
    ]
    request_path.write_text(json.dumps(request_for(baseline, candidate, targets)), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["intervention-create", str(request_path), "--out", str(exported)])
    assert result.exit_code == 0, result.output
    record = json.loads(exported.read_text(encoding="utf-8"))
    result = runner.invoke(
        cli, ["intervention-get", record["intervention_id"], "--out", str(fetched)]
    )
    assert result.exit_code == 0, result.output
    assert fetched.read_text(encoding="utf-8") == exported.read_text(encoding="utf-8")
    before, after, output = [
        tmp_path / name for name in ("before.json", "after.json", "failed.json")
    ]
    baseline.export_json(before)
    candidate.export_json(after)
    result = runner.invoke(
        cli,
        [
            "replay",
            "--baseline",
            str(before),
            "--candidate",
            str(after),
            "--out",
            str(tmp_path / "report.md"),
            "--min-latency-improvement-pct",
            "50",
            "--intervention-out",
            str(output),
            "--intervention-type",
            "test",
            "--target-finding",
            targets[0],
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(output.read_text(encoding="utf-8"))["gates_passed"] is False
