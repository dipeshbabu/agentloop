from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentloop.client import AgentLoopClient, AgentLoopClientError
from agentloop.demo import run_baseline
from agentloop.server import app


def test_client_upload_and_optimize(tmp_path, monkeypatch) -> None:
    path = run_baseline(tmp_path)
    test_client = TestClient(app)

    class LocalAgentLoopClient(AgentLoopClient):
        def _request(self, method, route, payload=None):  # type: ignore[no-untyped-def]
            if method == "GET" and route == "/health":
                return test_client.get(route).json()
            if method == "POST" and route == "/traces":
                return test_client.post(route, json=payload).json()
            if method == "GET" and route.endswith("/optimize"):
                return test_client.get(route).json()
            if method == "GET" and route == "/findings":
                return test_client.get(route).json()
            if method == "GET" and route == "/optimization-queue":
                return test_client.get(route).json()
            if method == "GET" and route.startswith("/optimization-queue/github-issues"):
                return test_client.get(route).json()
            if method == "POST" and route == "/quality-report":
                return test_client.post(route, json=payload).json()
            raise AssertionError(f"unexpected request {method} {route}")

    client = LocalAgentLoopClient()
    assert client.health()["status"] == "ok"
    uploaded = client.upload_trace(path)
    assert uploaded["ok"] is True
    plan = client.get_optimization_plan(uploaded["run_id"])
    assert len(plan["optimization_cards"]) >= 1
    assert client.list_findings()["findings"]
    assert client.optimization_queue()["queue"]
    assert client.github_issue_drafts()["issue_drafts"]
    quality = client.build_quality_report(
        [
            {
                "id": "exact",
                "candidate_output": "ok",
                "baseline_output": "ok",
                "expected": "ok",
                "scorer": {"type": "exact_match"},
            }
        ],
        min_score=1.0,
    )
    assert quality["passed"] is True


def test_client_quotes_run_ids_in_request_paths() -> None:
    routes: list[str] = []

    class CapturingClient(AgentLoopClient):
        def _request(self, method, route, payload=None):  # type: ignore[no-untyped-def]
            routes.append(route)
            return {}

    client = CapturingClient()
    client.get_report("team/run ?1")
    client.get_optimization_plan("team/run ?1")
    client.get_diagnosis("team/run ?1")
    client.get_value_report("team/run ?1")

    assert routes == [
        "/traces/team%2Frun%20%3F1/report",
        "/traces/team%2Frun%20%3F1/optimize",
        "/traces/team%2Frun%20%3F1/diagnose",
        "/traces/team%2Frun%20%3F1/value",
    ]


def test_client_rejects_non_http_api_urls() -> None:
    client = AgentLoopClient(base_url="file:///tmp/agentloop")

    with pytest.raises(AgentLoopClientError, match="must use http"):
        client.health()


def test_client_sends_only_configured_credential_headers(monkeypatch) -> None:
    captured_headers: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 10.0
        captured_headers.update({name.lower(): value for name, value in request.header_items()})
        return Response()

    monkeypatch.setattr("agentloop.client.urllib.request.urlopen", fake_urlopen)

    AgentLoopClient(api_key="user-secret").health()
    assert captured_headers["x-agentloop-key"] == "user-secret"
    assert "x-agentloop-admin-key" not in captured_headers

    captured_headers.clear()
    AgentLoopClient(admin_api_key="admin-secret").health()
    assert captured_headers["x-agentloop-admin-key"] == "admin-secret"
    assert "x-agentloop-key" not in captured_headers
