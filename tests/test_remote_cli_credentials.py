from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import agentloop.cli as cli_module
from agentloop.cli import app


def _capture_clients(monkeypatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    class CapturingClient:
        def __init__(
            self,
            *,
            base_url: str,
            api_key: str | None = None,
            admin_api_key: str | None = None,
        ) -> None:
            captured.append(
                {
                    "base_url": base_url,
                    "api_key": api_key,
                    "admin_api_key": admin_api_key,
                }
            )

        def upload_trace(self, path):  # type: ignore[no-untyped-def]
            return {"run_id": "run-1", "project_id": "project-1"}

        def get_optimization_plan(self, run_id):  # type: ignore[no-untyped-def]
            return {"run_id": run_id, "optimization_cards": []}

        def get_diagnosis(self, run_id):  # type: ignore[no-untyped-def]
            return {"run_id": run_id, "findings": []}

        def list_findings(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"findings": [], "filters": kwargs}

        def optimization_queue(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"queue": [], "filters": kwargs}

        def github_issue_drafts(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"issue_drafts": [], "filters": kwargs}

        def get_value_report(self, run_id, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "run_id": run_id,
                "monthly_value": {
                    "total_value_usd": 0,
                    "direct_model_cost_savings_usd": 0,
                    "engineering_hours_saved": 0,
                },
                "per_run": {"latency_savings_ms": 0, "cost_savings_usd": 0},
                "reliability": {"risk_score": 0},
                "pricing": {},
                "inputs": kwargs,
                "value_summary": "No modeled savings in this fixture.",
            }

        def usage_summary(self):
            return {"run_count": 0}

        def create_api_key(self, project_id, name):  # type: ignore[no-untyped-def]
            return {"api_key": "al_test", "project_id": project_id, "name": name}

    monkeypatch.setattr(cli_module, "AgentLoopClient", CapturingClient)
    return captured


@pytest.mark.parametrize(
    "command",
    [
        "upload",
        "remote-optimize",
        "remote-diagnose",
        "remote-findings",
        "remote-optimization-queue",
        "remote-github-issue-drafts",
        "remote-value-report",
        "remote-usage",
    ],
)
def test_remote_commands_use_environment_api_key(command, tmp_path, monkeypatch) -> None:
    captured = _capture_clients(monkeypatch)
    monkeypatch.setenv("AGENTLOOP_API_KEY", "from-environment")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "admin-must-not-be-sent")

    trace_path = tmp_path / "trace.json"
    trace_path.write_text("{}", encoding="utf-8")
    out_path = tmp_path / f"{command}.json"
    args_by_command = {
        "upload": ["upload", "--path", str(trace_path)],
        "remote-optimize": ["remote-optimize", "run-1", "--out", str(out_path)],
        "remote-diagnose": ["remote-diagnose", "run-1", "--out", str(out_path)],
        "remote-findings": ["remote-findings", "--out", str(out_path)],
        "remote-optimization-queue": [
            "remote-optimization-queue",
            "--out",
            str(out_path),
        ],
        "remote-github-issue-drafts": [
            "remote-github-issue-drafts",
            "--out",
            str(out_path),
        ],
        "remote-value-report": ["remote-value-report", "run-1", "--out", str(out_path)],
        "remote-usage": ["remote-usage"],
    }

    result = CliRunner().invoke(app, args_by_command[command])

    assert result.exit_code == 0, result.output
    assert captured == [
        {
            "base_url": "http://127.0.0.1:8000",
            "api_key": "from-environment",
            "admin_api_key": None,
        }
    ]


@pytest.mark.parametrize(
    ("environment_value", "command_options", "expected"),
    [
        ("from-environment", [], "from-environment"),
        ("from-environment", ["--api-key", "from-option"], "from-option"),
        (None, [], None),
    ],
)
def test_remote_api_key_precedence(
    environment_value, command_options, expected, monkeypatch
) -> None:
    captured = _capture_clients(monkeypatch)
    if environment_value is None:
        monkeypatch.delenv("AGENTLOOP_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENTLOOP_API_KEY", environment_value)

    result = CliRunner().invoke(app, ["remote-usage", *command_options])

    assert result.exit_code == 0, result.output
    assert captured[0]["api_key"] == expected
    assert captured[0]["admin_api_key"] is None


@pytest.mark.parametrize(
    ("environment_value", "command_options", "expected"),
    [
        ("from-environment", [], "from-environment"),
        ("from-environment", ["--admin-api-key", "from-option"], "from-option"),
        (None, [], None),
    ],
)
def test_remote_admin_api_key_precedence(
    environment_value, command_options, expected, monkeypatch
) -> None:
    captured = _capture_clients(monkeypatch)
    monkeypatch.setenv("AGENTLOOP_API_KEY", "user-must-not-be-sent")
    if environment_value is None:
        monkeypatch.delenv("AGENTLOOP_ADMIN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", environment_value)

    result = CliRunner().invoke(app, ["remote-create-api-key", *command_options])

    assert result.exit_code == 0, result.output
    assert captured[0]["admin_api_key"] == expected
    assert captured[0]["api_key"] is None
