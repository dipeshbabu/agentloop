from __future__ import annotations

from fastapi.testclient import TestClient

from agentloop.server import app
from agentloop.tracer import trace_agent, trace_model_call


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ingest_trace() -> None:
    client = TestClient(app)
    with trace_agent("server-test") as trace:
        with trace_model_call("call", input_tokens=12, output_tokens=4):
            pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["report"]["model_call_count"] == 1


def test_optimize_trace_endpoint() -> None:
    client = TestClient(app)
    with trace_agent("server-optimize-test") as trace:
        for _ in range(3):
            with trace_model_call("summarize", input_tokens=1000, output_tokens=100):
                pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    opt = client.get(f"/traces/{run_id}/optimize")
    assert opt.status_code == 200
    assert "optimization_cards" in opt.json()


def test_value_report_endpoint() -> None:
    client = TestClient(app)
    with trace_agent("server-value-test") as trace:
        for _ in range(3):
            with trace_model_call("summarize", input_tokens=1000, output_tokens=100):
                pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    value = client.get(f"/traces/{run_id}/value?runs_per_month=2500")
    assert value.status_code == 200
    body = value.json()
    assert body["assumptions"]["runs_per_month"] == 2500
    assert body["monthly_value"]["total_value_usd"] >= 0
    assert "sales_summary" in body


def test_api_key_auth(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_API_KEY", "test-token")
    client = TestClient(app)
    with trace_agent("auth-test") as trace:
        with trace_model_call("call", input_tokens=12, output_tokens=4):
            pass
    unauthorized = client.post("/traces", json=trace.to_dict())
    assert unauthorized.status_code == 401
    authorized = client.post("/traces", json=trace.to_dict(), headers={"X-AgentLoop-Key": "test-token"})
    assert authorized.status_code == 200
