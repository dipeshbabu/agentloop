from __future__ import annotations

from agentloop.client import AgentLoopClient
from agentloop.demo import run_baseline
from agentloop.server import app
from fastapi.testclient import TestClient


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
