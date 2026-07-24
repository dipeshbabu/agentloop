from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentloop.server import app
from agentloop.tracer import AgentTrace, trace_agent, trace_model_call


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


def test_readyz() -> None:
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_create_api_key_requires_admin_when_auth_is_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.delenv("AGENTLOOP_ADMIN_API_KEY", raising=False)

    client = TestClient(app)
    response = client.post("/api-keys", json={"project_id": "acme", "name": "prod"})

    assert response.status_code == 503


def test_create_api_key_accepts_admin_key(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-secret")

    client = TestClient(app)
    rejected = client.post(
        "/api-keys",
        json={"project_id": "acme", "name": "prod"},
        headers={"X-AgentLoop-Admin-Key": "wrong"},
    )
    accepted = client.post(
        "/api-keys",
        json={"project_id": "acme", "name": "prod"},
        headers={"X-AgentLoop-Admin-Key": "admin-secret"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["api_key"].startswith("al_")


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


def test_ingest_trace_preserves_explicit_elapsed_runtime() -> None:
    client = TestClient(app)
    trace = AgentTrace(
        "server-elapsed-test",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00.125000+00:00",
        elapsed_ms=125,
    )

    response = client.post("/traces", json=trace.to_dict())

    assert response.status_code == 200
    assert response.json()["report"]["total_runtime_ms"] == 125
    assert response.json()["report"]["cumulative_span_time_ms"] == 0


def test_ingest_trace_serializes_schema_version() -> None:
    with trace_agent("schema-version") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    from agentloop.schema import SCHEMA_VERSION

    assert trace.to_dict()["schema_version"] == SCHEMA_VERSION


def test_ingest_malformed_event_returns_422_not_500() -> None:
    client = TestClient(app)
    response = client.post(
        "/traces",
        json={"name": "bad", "run_id": "run_bad", "events": [{"unexpected": True}]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["field"].startswith("events[0]")
    assert detail["reason"]


def test_ingest_negative_tokens_returns_422() -> None:
    client = TestClient(app)
    with trace_agent("neg-tokens") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    payload = trace.to_dict()
    payload["events"][0]["input_tokens"] = -5

    response = client.post("/traces", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "events[0].input_tokens"


def test_ingest_event_run_id_mismatch_returns_422() -> None:
    client = TestClient(app)
    with trace_agent("mismatch") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    payload = trace.to_dict()
    payload["events"][0]["run_id"] = "run_somethingelse"

    response = client.post("/traces", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "events[0].run_id"


def test_ingest_future_schema_version_returns_422() -> None:
    client = TestClient(app)
    with trace_agent("future") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    payload = trace.to_dict()
    payload["schema_version"] = "999.0"

    response = client.post("/traces", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "schema_version"


def test_quality_report_string_scorer_is_4xx_not_500() -> None:
    client = TestClient(app)
    response = client.post(
        "/quality-report",
        json={"fixtures": [{"candidate_output": "ok", "scorer": "exact_match"}]},
    )
    assert 400 <= response.status_code < 500
    assert "object" in response.json()["detail"]


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


def test_diagnose_trace_endpoint() -> None:
    client = TestClient(app)
    with trace_agent("server-diagnose-test") as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    diagnosis = client.get(f"/traces/{run_id}/diagnose")

    assert diagnosis.status_code == 200
    body = diagnosis.json()
    assert body["summary"]["finding_count"] >= 1
    assert "findings" in body


def test_findings_and_optimization_queue_endpoints() -> None:
    client = TestClient(app)
    with trace_agent("server-queue-test") as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200

    findings = client.get("/findings")
    queue = client.get("/optimization-queue")
    issues = client.get("/optimization-queue/github-issues")

    assert findings.status_code == 200
    assert queue.status_code == 200
    assert issues.status_code == 200
    assert findings.json()["findings"]
    assert queue.json()["queue"]
    assert issues.json()["issue_drafts"]


def test_quality_report_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        "/quality-report",
        json={
            "fixtures": [
                {
                    "id": "fields",
                    "candidate_output": {"summary": "ok", "sources": ["a"]},
                    "baseline_output": {"summary": "ok", "sources": ["a"]},
                    "scorer": {"type": "required_fields", "required": ["summary", "sources"]},
                }
            ],
            "min_score": 1.0,
        },
    )

    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_quality_report_endpoint_fails_falsey_type_and_missing_key_cases() -> None:
    client = TestClient(app)
    response = client.post(
        "/quality-report",
        json={
            "fixtures": [
                {"candidate_output": False, "expected": 0},
                {
                    "candidate_output": {},
                    "scorer": {
                        "type": "json_subset",
                        "expected": {"result": None},
                    },
                },
            ],
            "min_score": 1,
        },
    )

    assert response.status_code == 200
    report = response.json()
    assert report["passed"] is False
    assert report["candidate_score"] == 0.0
    assert report["failed_case_count"] == 2


def test_quality_report_endpoint_rejects_missing_or_empty_fixtures() -> None:
    client = TestClient(app)

    missing = client.post("/quality-report", json={})
    empty = client.post("/quality-report", json={"fixtures": []})

    assert missing.status_code == 422
    assert empty.status_code == 422
    assert "at least one case" in empty.json()["detail"]


def test_quality_report_endpoint_rejects_vacuous_scorer_and_invalid_score() -> None:
    client = TestClient(app)

    scorer = client.post(
        "/quality-report",
        json={"fixtures": [{"candidate_output": "anything", "scorer": {"type": "contains"}}]},
    )
    score = client.post(
        "/quality-report",
        json={
            "fixtures": [{"candidate_output": "ok", "expected": "ok"}],
            "min_score": 1.01,
        },
    )
    boolean_score = client.post(
        "/quality-report",
        json={
            "fixtures": [{"candidate_output": "ok", "expected": "ok"}],
            "min_score": True,
        },
    )
    string_score = client.post(
        "/quality-report",
        json={
            "fixtures": [{"candidate_output": "ok", "expected": "ok"}],
            "min_score": "1.0",
        },
    )

    assert scorer.status_code == 422
    assert "non-empty" in scorer.json()["detail"]
    assert score.status_code == 422
    assert boolean_score.status_code == 422
    assert string_score.status_code == 422


def test_quality_report_endpoint_rejects_custom_python_scorers() -> None:
    client = TestClient(app)
    for scorer_type in ("custom", " custom "):
        response = client.post(
            "/quality-report",
            json={
                "fixtures": [
                    {
                        "id": "unsafe",
                        "candidate_output": "ok",
                        "scorer": {"type": scorer_type, "callable": "example:score"},
                    }
                ]
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "custom Python scorers are not accepted by the HTTP API"
        )


def test_quality_report_endpoint_rejects_raw_regex_scorers() -> None:
    client = TestClient(app)
    for scorer_type in ("regex", " regex "):
        response = client.post(
            "/quality-report",
            json={
                "fixtures": [
                    {
                        "id": "unsafe-regex",
                        "candidate_output": "a" * 10_000 + "!",
                        "scorer": {"type": scorer_type, "pattern": "(a+)+$"},
                    }
                ]
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "raw regex scorers are not accepted; use glob, contains, or exact_match"
        )


def test_value_report_endpoint() -> None:
    client = TestClient(app)
    with trace_agent("server-value-test") as trace:
        for _ in range(3):
            with trace_model_call(
                "summarize",
                model="gpt-4o",
                input_tokens=1000,
                output_tokens=100,
            ):
                pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    value = client.get(f"/traces/{run_id}/value?runs_per_month=2500")
    assert value.status_code == 200
    body = value.json()
    assert body["assumptions"]["runs_per_month"] == 2500
    assert body["monthly_value"]["total_value_usd"] >= 0


def test_value_report_endpoint_withholds_unknown_cost_totals() -> None:
    client = TestClient(app)
    with trace_agent("server-unknown-value-test") as trace:
        with trace_model_call(
            "summarize", model="private-model", input_tokens=1000, output_tokens=100
        ):
            pass
    response = client.post("/traces", json=trace.to_dict())
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    value = client.get(f"/traces/{run_id}/value?runs_per_month=2500")

    assert value.status_code == 200
    body = value.json()
    assert body["cost_status"] == "unknown"
    assert body["monthly_value"]["direct_model_cost_savings_usd"] is None
    assert body["monthly_value"]["total_value_usd"] is None
    assert body["pricing"]["suggested_plan"] is None
    assert "value_summary" in body


def test_api_key_auth(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_API_KEY", "test-token")
    client = TestClient(app)
    with trace_agent("auth-test") as trace:
        with trace_model_call("call", input_tokens=12, output_tokens=4):
            pass
    unauthorized = client.post("/traces", json=trace.to_dict())
    assert unauthorized.status_code == 401
    authorized = client.post(
        "/traces", json=trace.to_dict(), headers={"X-AgentLoop-Key": "test-token"}
    )
    assert authorized.status_code == 200


def _create_project_key(client: TestClient, project_id: str) -> str:
    response = client.post(
        "/api-keys",
        json={"project_id": project_id, "name": "test"},
        headers={"X-AgentLoop-Admin-Key": "admin-secret"},
    )
    assert response.status_code == 200
    return str(response.json()["api_key"])


def test_project_key_cannot_override_project_filter(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-secret")
    client = TestClient(app)
    alpha_key = _create_project_key(client, "alpha")

    for path in (
        "/traces",
        "/findings",
        "/optimization-queue",
        "/optimization-queue/github-issues",
        "/usage",
    ):
        response = client.get(
            f"{path}?project_id=beta",
            headers={"X-AgentLoop-Key": alpha_key},
        )
        assert response.status_code == 403, path


def test_run_id_cannot_move_between_projects(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "ownership.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-secret")
    client = TestClient(app)
    alpha_key = _create_project_key(client, "alpha")
    beta_key = _create_project_key(client, "beta")

    with trace_agent("project-ownership-test") as trace:
        with trace_model_call("call", input_tokens=4, output_tokens=2):
            pass

    created = client.post(
        "/traces",
        json=trace.to_dict(),
        headers={"X-AgentLoop-Key": alpha_key},
    )
    conflict = client.post(
        "/traces",
        json=trace.to_dict(),
        headers={"X-AgentLoop-Key": beta_key},
    )

    assert created.status_code == 200
    assert conflict.status_code == 409
    assert "already belongs to another project" in conflict.json()["detail"]


def test_list_traces_without_page_size_is_backward_compatible(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "pagination.db"))
    client = TestClient(app)
    with trace_agent("compat-test") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    client.post("/traces", json=trace.to_dict())

    response = client.get("/traces")

    assert response.status_code == 200
    body = response.json()
    assert body["traces"][0]["run_id"] == trace.run_id
    assert body["next_cursor"] is None


def test_list_traces_page_size_paginates_and_walks_cursor(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "pagination.db"))
    client = TestClient(app)
    run_ids = set()
    for index in range(5):
        with trace_agent(f"page-test-{index}") as trace:
            with trace_model_call("call", input_tokens=1, output_tokens=1):
                pass
        client.post("/traces", json=trace.to_dict())
        run_ids.add(trace.run_id)

    seen = []
    cursor = None
    for _ in range(10):
        response = client.get(
            "/traces", params={"page_size": 2, **({"cursor": cursor} if cursor else {})}
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["traces"]) <= 2
        seen.extend(item["run_id"] for item in body["traces"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert set(seen) == run_ids
    assert len(seen) == len(set(seen))


def test_list_traces_rejects_invalid_cursor(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "pagination.db"))
    client = TestClient(app)

    response = client.get("/traces", params={"page_size": 5, "cursor": "not-a-real-cursor"})

    assert response.status_code == 400


def test_list_findings_page_size_and_default_compatibility(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "pagination.db"))
    client = TestClient(app)
    repeated = "stable context " * 100
    for index in range(2):
        with trace_agent(f"findings-page-test-{index}") as trace:
            for _ in range(2):
                with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                    pass
        client.post("/traces", json=trace.to_dict())

    default_response = client.get("/findings")
    paged_response = client.get("/findings", params={"page_size": 1})

    assert default_response.status_code == 200
    assert default_response.json()["next_cursor"] is None
    assert paged_response.status_code == 200
    assert len(paged_response.json()["findings"]) == 1
    assert paged_response.json()["next_cursor"] is not None


def test_update_finding_status_transition_lifecycle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "lifecycle.db"))
    client = TestClient(app)
    with trace_agent("lifecycle-test") as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
    client.post("/traces", json=trace.to_dict())
    finding = client.get("/findings").json()["findings"][0]
    run_id, finding_id = finding["run_id"], finding["finding_id"]

    accepted = client.post(f"/findings/{run_id}/{finding_id}/status", json={"status": "accepted"})
    resolved = client.post(f"/findings/{run_id}/{finding_id}/status", json={"status": "resolved"})
    conflict = client.post(f"/findings/{run_id}/{finding_id}/status", json={"status": "accepted"})
    not_found = client.post(
        f"/findings/{run_id}/does-not-exist/status", json={"status": "accepted"}
    )
    bad_status = client.post(f"/findings/{run_id}/{finding_id}/status", json={"status": "bogus"})

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert conflict.status_code == 409
    assert not_found.status_code == 404
    assert bad_status.status_code == 400
