from __future__ import annotations

import json

import pytest
from workflow_fixtures import pipeline

from agentloop.store import SQLiteTraceStore
from agentloop.workflow_types import STAGE_KEY, WORKFLOW_KEY


def test_workflow_uses_existing_sqlite_storage_and_legacy_trace_contract(tmp_path):
    store = SQLiteTraceStore(str(tmp_path / "workflows.db"))
    trace = pipeline(branching=True)
    store.save_trace(trace)
    loaded = store.get_trace(trace.run_id)
    assert loaded.to_dict() == trace.to_dict()
    assert loaded.report()["execution"]["task_id"] == "task-1"
    assert loaded.report()["stages"]["route"]["depends_on"] == ["lookup", "priority"]


def test_existing_http_routes_preserve_workflow_and_stage_fields(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentloop.server import app

    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "http-workflow.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    trace = pipeline(branching=True)
    client = TestClient(app)
    assert client.post("/v1/traces", json=trace.to_dict()).status_code == 200
    report = client.get(f"/v1/traces/{trace.run_id}/report")
    assert report.status_code == 200
    payload = report.json()
    assert payload["execution"]["workflow_id"] == "mail-router"
    assert payload["stages"]["route"]["kind"] == "external_service"
    diagnosis = client.get(f"/v1/traces/{trace.run_id}/diagnosis")
    assert diagnosis.status_code == 200
    assert diagnosis.json()["graph"]["dependency_evidence"]["valid"]
    store = SQLiteTraceStore(str(tmp_path / "http-workflow.db"))
    loaded = store.get_trace(trace.run_id)
    assert loaded.metadata[WORKFLOW_KEY] == trace.metadata[WORKFLOW_KEY]
    assert loaded.events[0].metadata[STAGE_KEY] == trace.events[0].metadata[STAGE_KEY]


def test_workflow_upload_roundtrip_uses_the_existing_dashboard_parser():
    pytest.importorskip("streamlit")
    from dashboard.trace_upload import parse_uploaded_trace

    trace = pipeline()
    loaded = parse_uploaded_trace(json.dumps(trace.to_dict()).encode())
    assert loaded.to_dict() == trace.to_dict()
