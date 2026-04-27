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
            raise AssertionError(f"unexpected request {method} {route}")

    client = LocalAgentLoopClient()
    assert client.health()["status"] == "ok"
    uploaded = client.upload_trace(path)
    assert uploaded["ok"] is True
    plan = client.get_optimization_plan(uploaded["run_id"])
    assert len(plan["optimization_cards"]) >= 1
