from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.tracer import AgentTrace

runner = CliRunner()


def test_quickstart_generates_offline_trace_and_findings(tmp_path) -> None:
    trace_path = tmp_path / "quickstart.json"
    analysis_path = tmp_path / "analysis.json"

    result = runner.invoke(
        app,
        ["quickstart", "--out", str(trace_path), "--json-out", str(analysis_path)],
    )

    assert result.exit_code == 0, result.output
    trace = AgentTrace.from_json(trace_path)
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert trace.metadata == {"synthetic": True, "source": "agentloop_quickstart"}
    assert payload["diagnosis"]["summary"]["finding_count"] > 0
    assert payload["optimization"]["optimization_cards"]
    assert "Top findings" in result.output
    assert "Next: agentloop analyze" in result.output


def test_analyze_combines_report_diagnosis_and_optimization(tmp_path) -> None:
    trace_path = tmp_path / "quickstart.json"
    result = runner.invoke(app, ["quickstart", "--out", str(trace_path)])
    assert result.exit_code == 0, result.output

    analysis_path = tmp_path / "analysis.json"
    result = runner.invoke(
        app,
        ["analyze", str(trace_path), "--json-out", str(analysis_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert set(payload) == {"trace", "report", "diagnosis", "optimization"}
    assert payload["trace"]["run_id"] == "run_agentloop_quickstart"
    assert payload["diagnosis"]["summary"]["finding_count"] > 0


def test_analyze_rejects_missing_trace(tmp_path) -> None:
    result = runner.invoke(app, ["analyze", str(tmp_path / "missing.json")])

    assert result.exit_code != 0
    assert "does not exist" in result.output
