from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import agentloop.rules as rules
from agentloop.entrypoint import _quickstart_trace
from agentloop.server import app
from agentloop.store import SQLiteTraceStore


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize(
    "operation", ["ingest", "report", "optimization", "diagnosis", "save-diagnosis"]
)
def test_internal_rule_failures_never_expose_details_through_api(
    tmp_path, monkeypatch, caplog, prefix, operation
):
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "privacy.db"))
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "false")
    trace = _quickstart_trace()
    SQLiteTraceStore(str(tmp_path / "privacy.db")).save_trace(trace)
    secret = "private-diagnostic-value-123"
    private_error = type("PrivateImplementationFailure", (Exception,), {})

    def broken(context):
        raise private_error(secret)

    monkeypatch.setattr(rules, "BUILTIN_RULES", (rules.FindingRule("failing-rule", "1.0", broken),))
    client = TestClient(app)
    with caplog.at_level(logging.DEBUG, logger="agentloop.rules"):
        if operation == "ingest":
            response = client.post(prefix + "/traces", json=trace.to_dict())
        else:
            resource = "optimize" if operation == "optimization" and not prefix else operation
            resource = "diagnosis" if operation == "save-diagnosis" else resource
            method = "POST" if operation == "save-diagnosis" else "GET"
            response = client.request(method, f"{prefix}/traces/{trace.run_id}/{resource}")
    assert response.status_code == 200
    assert secret not in response.text
    assert "PrivateImplementationFailure" not in response.text
    assert "Traceback" not in response.text
    data = response.json()["report"] if operation == "ingest" else response.json()
    assert data["analysis_complete"] is False
    assert data["rule_errors"] == [
        {
            "rule_id": "failing-rule",
            "rule_version": "1.0",
            "error_type": "RuleError",
            "message": "Rule evaluation failed; see local debug logs.",
        }
    ]
    assert secret in caplog.text


def test_rule_failures_do_not_log_private_details_at_default_level(monkeypatch, caplog):
    def broken(context):
        raise ValueError("private-default-log-value")

    monkeypatch.setattr(rules, "BUILTIN_RULES", (rules.FindingRule("broken", "1.0", broken),))
    with caplog.at_level(logging.INFO, logger="agentloop.rules"):
        report = _quickstart_trace().report()
    assert "private-default-log-value" not in str(report)
    assert "private-default-log-value" not in caplog.text
    assert report["rule_errors"][0]["error_type"] == "ValueError"
