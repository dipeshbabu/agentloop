from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentloop.client import AgentLoopClient, AgentLoopClientError
from agentloop.demo import run_baseline
from agentloop.server import app


def _capture_request_headers(monkeypatch) -> list[dict[str, str]]:
    captured: list[dict[str, str]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 10.0
        captured.append({name.lower(): value for name, value in request.header_items()})
        return Response()

    monkeypatch.setattr("agentloop.client.urllib.request.urlopen", fake_urlopen)
    return captured


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
    client.save_diagnosis("team/run ?1")
    client.get_value_report("team/run ?1")
    client.update_finding_status("team/run ?1", "finding/1", "accepted")

    assert routes == [
        "/traces/team%2Frun%20%3F1/report",
        "/traces/team%2Frun%20%3F1/optimize",
        "/traces/team%2Frun%20%3F1/diagnosis",
        "/traces/team%2Frun%20%3F1/diagnosis",
        "/traces/team%2Frun%20%3F1/value",
        "/findings/team%2Frun%20%3F1/finding%2F1/status",
    ]


def test_client_pagination_and_finding_status_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "client.db"))
    test_client = TestClient(app)

    class LocalAgentLoopClient(AgentLoopClient):
        def _request(self, method, route, payload=None):  # type: ignore[no-untyped-def]
            if method == "GET":
                return test_client.get(route).json()
            if method == "POST":
                return test_client.post(route, json=payload).json()
            raise AssertionError(f"unexpected request {method} {route}")

    client = LocalAgentLoopClient()
    path = run_baseline(tmp_path)
    uploaded = client.upload_trace(path)
    run_id = uploaded["run_id"]

    default_page = client.list_traces()
    assert default_page["next_cursor"] is None
    assert default_page["traces"][0]["run_id"] == run_id

    paged = client.list_traces(page_size=1)
    assert len(paged["traces"]) == 1

    finding = client.list_findings()["findings"][0]
    updated = client.update_finding_status(finding["run_id"], finding["finding_id"], "accepted")
    assert updated["status"] == "accepted"


def test_client_rejects_non_http_api_urls() -> None:
    client = AgentLoopClient(base_url="file:///tmp/agentloop")

    with pytest.raises(AgentLoopClientError, match="must use http"):
        client.health()


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda client: client.health(), id="health"),
        pytest.param(
            lambda client: client.upload_trace({"name": "trace", "run_id": "run", "events": []}),
            id="upload-trace",
        ),
        pytest.param(lambda client: client.list_traces(), id="list-traces"),
        pytest.param(lambda client: client.usage_summary(), id="usage"),
        pytest.param(lambda client: client.get_report("run"), id="report"),
        pytest.param(lambda client: client.get_optimization_plan("run"), id="optimize"),
        pytest.param(lambda client: client.get_diagnosis("run"), id="diagnose"),
        pytest.param(lambda client: client.save_diagnosis("run"), id="persist-diagnosis"),
        pytest.param(lambda client: client.list_findings(), id="list-findings"),
        pytest.param(
            lambda client: client.update_finding_status("run", "finding", "accepted"),
            id="update-finding",
        ),
        pytest.param(lambda client: client.optimization_queue(), id="optimization-queue"),
        pytest.param(lambda client: client.github_issue_drafts(), id="github-issue-drafts"),
        pytest.param(
            lambda client: client.build_quality_report(
                [{"candidate_output": "ok", "expected": "ok"}]
            ),
            id="quality-report",
        ),
        pytest.param(lambda client: client.get_value_report("run"), id="value-report"),
    ],
)
def test_client_sends_only_project_key_on_ordinary_requests(monkeypatch, operation) -> None:
    captured = _capture_request_headers(monkeypatch)
    client = AgentLoopClient(api_key="project-secret", admin_api_key="admin-secret")

    operation(client)

    assert len(captured) == 1
    assert captured[0]["x-agentloop-key"] == "project-secret"
    assert "x-agentloop-admin-key" not in captured[0]


def test_client_sends_only_admin_key_for_api_key_creation(monkeypatch) -> None:
    captured = _capture_request_headers(monkeypatch)
    client = AgentLoopClient(api_key="project-secret", admin_api_key="admin-secret")

    client.create_api_key(project_id="project", name="deployment")

    assert captured == [
        {
            "accept": "application/json",
            "content-type": "application/json",
            "x-agentloop-admin-key": "admin-secret",
        }
    ]
    assert "x-agentloop-key" not in captured[0]


def test_client_does_not_substitute_credentials_between_scopes(monkeypatch) -> None:
    captured = _capture_request_headers(monkeypatch)

    AgentLoopClient(admin_api_key="admin-secret").health()
    AgentLoopClient(api_key="project-secret").create_api_key()

    assert "x-agentloop-admin-key" not in captured[0]
    assert "x-agentloop-key" not in captured[0]
    assert "x-agentloop-admin-key" not in captured[1]
    assert "x-agentloop-key" not in captured[1]


def test_client_rejects_unknown_credential_scopes(monkeypatch) -> None:
    captured = _capture_request_headers(monkeypatch)

    with pytest.raises(AgentLoopClientError, match="Unsupported credential scope"):
        AgentLoopClient(api_key="project-secret", admin_api_key="admin-secret")._request(
            "GET",
            "/health",
            credential_scope="unknown",  # type: ignore[arg-type]
        )

    assert captured == []


def test_client_from_env_routes_each_credential_to_its_scope(monkeypatch) -> None:
    captured = _capture_request_headers(monkeypatch)
    monkeypatch.setenv("AGENTLOOP_API_KEY", "project-secret")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-secret")
    client = AgentLoopClient.from_env()

    client.health()
    client.create_api_key()

    assert captured[0]["x-agentloop-key"] == "project-secret"
    assert "x-agentloop-admin-key" not in captured[0]
    assert captured[1]["x-agentloop-admin-key"] == "admin-secret"
    assert "x-agentloop-key" not in captured[1]
