from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agentloop.server as server
from agentloop.client import AgentLoopClient
from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace


def _finding(finding_id: str, title: str = "Original finding") -> dict:
    return {
        "finding_id": finding_id,
        "type": "cache_context",
        "severity": "high",
        "confidence": "high",
        "title": title,
    }


@pytest.fixture
def diagnosis_api(tmp_path, monkeypatch):
    database = tmp_path / "diagnosis.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    db = SQLiteTraceStore(path=str(database))
    trace = AgentTrace(name="diagnosis-state")
    db.save_trace(trace)
    db.save_diagnosis(
        {
            "run_id": trace.run_id,
            "findings": [_finding(name) for name in ("kept", "stale", "resolved", "dismissed")],
        }
    )
    db.update_finding_status("default", trace.run_id, "kept", "accepted")
    db.update_finding_status("default", trace.run_id, "resolved", "accepted")
    db.update_finding_status("default", trace.run_id, "resolved", "resolved")
    db.update_finding_status("default", trace.run_id, "dismissed", "dismissed")
    # Simulate changed analyzer output so an accidental write deterministically
    # inserts, updates, and supersedes rows without relying on clock timing.
    diagnosis = {
        "run_id": trace.run_id,
        "findings": [_finding("kept", "Updated finding"), _finding("new")],
    }
    monkeypatch.setattr(server, "build_diagnosis", lambda trace: diagnosis)
    return TestClient(server.app), db, trace.run_id, diagnosis


@pytest.mark.parametrize("route", ["diagnose", "diagnosis"])
def test_get_diagnosis_never_mutates_persisted_findings(diagnosis_api, route) -> None:
    client, db, run_id, diagnosis = diagnosis_api
    before = db.list_findings(project_id="default")

    for _ in range(2):
        response = client.get(f"/traces/{run_id}/{route}")
        assert response.status_code == 200
        assert response.json() == diagnosis
        assert db.list_findings(project_id="default") == before


def test_post_diagnosis_persists_and_preserves_finding_decisions(diagnosis_api) -> None:
    client, db, run_id, diagnosis = diagnosis_api

    for _ in range(2):
        response = client.post(f"/traces/{run_id}/diagnosis")
        assert response.status_code == 200
        assert response.json() == diagnosis
        findings = {row["finding_id"]: row for row in db.list_findings(project_id="default")}
        assert {key: row["status"] for key, row in findings.items()} == {
            "kept": "accepted",
            "stale": "superseded",
            "resolved": "resolved",
            "dismissed": "dismissed",
            "new": "detected",
        }
        assert findings["kept"]["title"] == "Updated finding"


@pytest.mark.parametrize(
    ("method", "route"), [("GET", "diagnose"), ("GET", "diagnosis"), ("POST", "diagnosis")]
)
def test_diagnosis_operations_preserve_auth_and_project_isolation(
    diagnosis_api, monkeypatch, method, route
) -> None:
    client, db, run_id, _ = diagnosis_api
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    other_key = db.create_api_key("other", "test")["api_key"]
    own_key = db.create_api_key("default", "test")["api_key"]
    before = db.list_findings()

    missing_key = client.request(method, f"/traces/{run_id}/{route}")
    wrong_project = client.request(
        method, f"/traces/{run_id}/{route}", headers={"X-AgentLoop-Key": other_key}
    )
    missing_run = client.request(
        method, f"/traces/missing/{route}", headers={"X-AgentLoop-Key": own_key}
    )

    assert missing_key.status_code == 401
    assert wrong_project.status_code == 404
    assert wrong_project.json() == {"detail": "trace not found"}
    assert missing_run.status_code == 404
    assert missing_run.json() == {"detail": "trace not found"}
    assert db.list_findings() == before
    authorized = client.request(
        method, f"/traces/{run_id}/{route}", headers={"X-AgentLoop-Key": own_key}
    )
    assert authorized.status_code == 200


def test_python_client_distinguishes_read_and_persist(diagnosis_api) -> None:
    http, db, run_id, diagnosis = diagnosis_api

    class LocalClient(AgentLoopClient):
        def _request(self, method, route, payload=None):
            response = http.request(method, route, json=payload)
            response.raise_for_status()
            return response.json()

    client = LocalClient()
    before = db.list_findings()
    assert client.get_diagnosis(run_id) == diagnosis
    assert db.list_findings() == before
    assert client.save_diagnosis(run_id) == diagnosis
    assert {row["finding_id"] for row in db.list_findings()} == {
        "kept",
        "stale",
        "resolved",
        "dismissed",
        "new",
    }


def test_openapi_marks_only_legacy_diagnosis_route_deprecated() -> None:
    paths = server.app.openapi()["paths"]
    assert paths["/traces/{run_id}/diagnose"]["get"]["deprecated"] is True
    assert not paths["/traces/{run_id}/diagnosis"]["get"].get("deprecated", False)
    assert "post" in paths["/traces/{run_id}/diagnosis"]
