from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from agentloop.client import AgentLoopClient
from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis
from agentloop.server import app, legacy, versioned
from agentloop.store import SQLiteTraceStore
from agentloop.version import __version__


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "versioned.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    db = SQLiteTraceStore(str(tmp_path / "versioned.db"))
    trace = _quickstart_trace()
    db.save_trace(trace)
    return TestClient(app), db, trace


@pytest.mark.parametrize(
    "old,new",
    [
        ("/health", "/v1/health"),
        ("/readyz", "/v1/readyz"),
        ("/traces", "/v1/traces"),
        ("/findings", "/v1/findings"),
        ("/optimization-queue", "/v1/optimization-queue"),
        ("/optimization-queue/github-issues", "/v1/optimization-queue/github-issues"),
        ("/usage", "/v1/usage"),
        ("/traces/{run}/report", "/v1/traces/{run}/report"),
        ("/traces/{run}/optimize", "/v1/traces/{run}/optimization"),
        ("/traces/{run}/diagnose", "/v1/traces/{run}/diagnosis"),
        ("/traces/{run}/diagnosis", "/v1/traces/{run}/diagnosis"),
        ("/traces/{run}/value", "/v1/traces/{run}/value"),
    ],
)
def test_read_routes_have_identical_legacy_behavior(api, old, new):
    client, _, trace = api
    before = client.get(old.format(run=trace.run_id))
    after = client.get(new.format(run=trace.run_id))
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()


def test_writes_and_intervention_retrieval_share_the_same_contract(api):
    client, _, baseline = api
    candidate = copy.deepcopy(baseline)
    candidate.run_id = "versioned-candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    old = client.post("/traces", json=candidate.to_dict())
    new = client.post("/v1/traces", json=candidate.to_dict())
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    old = client.post(f"/traces/{baseline.run_id}/diagnosis")
    new = client.post(f"/v1/traces/{baseline.run_id}/diagnosis")
    assert old.json() == new.json()
    request = {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "target_finding_ids": [build_diagnosis(baseline)["findings"][0]["finding_id"]],
        "intervention_type": "test",
    }
    old = client.post("/interventions", json=request)
    new = client.post("/v1/interventions", json=request)
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    record_id = new.json()["intervention_id"]
    assert (
        client.get(f"/interventions/{record_id}").json()
        == client.get(f"/v1/interventions/{record_id}").json()
    )
    quality = {"fixtures": [{"expected": "ok", "candidate_output": "ok", "baseline_output": "ok"}]}
    old = client.post("/quality-report", json=quality)
    new = client.post("/v1/quality-reports", json=quality)
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()


@pytest.mark.parametrize("route", ["traces", "findings"])
def test_pagination_and_cursor_errors_are_identical(api, route):
    client, _, _ = api
    assert (
        client.get(f"/{route}?page_size=1").json() == client.get(f"/v1/{route}?page_size=1").json()
    )
    old = client.get(f"/{route}?page_size=1&cursor=invalid")
    new = client.get(f"/v1/{route}?page_size=1&cursor=invalid")
    assert old.status_code == new.status_code == 400
    assert old.json() == new.json()


def test_auth_project_isolation_and_admin_scopes_remain_unchanged(api, monkeypatch):
    client, db, trace = api
    key = db.create_api_key("other", "other")["api_key"]
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-only")
    for prefix in ("", "/v1"):
        assert client.get(prefix + "/traces").status_code == 401
        assert (
            client.get(
                prefix + f"/traces/{trace.run_id}/report", headers={"X-AgentLoop-Key": key}
            ).status_code
            == 404
        )
        assert (
            client.get(
                prefix + "/traces?project_id=default", headers={"X-AgentLoop-Key": key}
            ).status_code
            == 403
        )
        assert (
            client.post(prefix + "/api-keys", json={}, headers={"X-AgentLoop-Key": key}).status_code
            == 401
        )
        assert (
            client.post(
                prefix + "/api-keys", json={}, headers={"X-AgentLoop-Admin-Key": "admin-only"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "method,old,new,body",
    [
        ("GET", "/traces/missing/report", "/v1/traces/missing/report", None),
        ("POST", "/traces", "/v1/traces", {"name": "bad"}),
        ("POST", "/quality-report", "/v1/quality-reports", {"fixtures": []}),
        (
            "POST",
            "/findings/missing/missing/status",
            "/v1/findings/missing/missing/status",
            {"status": "accepted"},
        ),
    ],
)
def test_error_payloads_match(api, method, old, new, body):
    client, _, _ = api
    before, after = client.request(method, old, json=body), client.request(method, new, json=body)
    assert before.status_code == after.status_code
    assert before.status_code >= 400
    assert before.json() == after.json()


def test_openapi_marks_aliases_and_shares_endpoint_implementations():
    schema = app.openapi()
    assert schema["info"]["version"] == __version__
    assert "earliest removal is 1.0" in schema["info"]["description"]
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            if path.startswith("/v1/"):
                assert not operation.get("deprecated", False)
                assert "API v1" in operation["tags"]
            elif path not in {"/health", "/readyz"}:
                assert operation["deprecated"] is True
    routes = {
        (method, prefix + route.path): route.endpoint
        for router, prefix in ((legacy, ""), (versioned, "/v1"))
        for route in router.routes
        for method in route.methods
    }
    for old, new in (
        ("/quality-report", "/v1/quality-reports"),
        ("/traces/{run_id}/optimize", "/v1/traces/{run_id}/optimization"),
        ("/traces/{run_id}/diagnose", "/v1/traces/{run_id}/diagnosis"),
        ("/interventions", "/v1/interventions"),
    ):
        method = "POST" if old in {"/quality-report", "/interventions"} else "GET"
        assert routes[(method, old)] is routes[(method, new)]


def test_real_client_request_adds_v1_once_under_the_configured_mount(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"{}"

    def capture(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr("agentloop.client.urllib.request.urlopen", capture)
    client = AgentLoopClient(
        base_url="https://example.test/mount/", api_key="project", admin_api_key="admin"
    )
    client.get_optimization_plan("run ?1")
    client.build_quality_report([])
    client.create_intervention({"intervention_type": "test"})
    assert [request.full_url for request in requests] == [
        "https://example.test/mount/v1/traces/run%20%3F1/optimization",
        "https://example.test/mount/v1/quality-reports",
        "https://example.test/mount/v1/interventions",
    ]
    assert json.loads(requests[2].data) == {"intervention_type": "test"}
    assert all(request.get_header("X-agentloop-key") == "project" for request in requests)
    assert all(request.get_header("X-agentloop-admin-key") is None for request in requests)
