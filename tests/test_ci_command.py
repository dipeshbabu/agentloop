from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.cli import app
from agentloop.events import AgentEvent, utc_now_iso
from agentloop.replay import ReplayGates
from agentloop.tracer import AgentTrace


def _trace(name: str, *, duration_ms: float, input_tokens: int, retries: int = 0) -> AgentTrace:
    trace = AgentTrace(name=name)
    now = utc_now_iso()
    trace.add_event(
        AgentEvent(
            event_id=f"evt_{name}_model",
            run_id=trace.run_id,
            event_type="model_call",
            name="plan",
            started_at=now,
            ended_at=now,
            duration_ms=duration_ms,
            model="gpt-4.1-mini",
            input_tokens=input_tokens,
            output_tokens=100,
        )
    )
    for index in range(3):
        trace.add_event(
            AgentEvent(
                event_id=f"evt_{name}_tool_{index}",
                run_id=trace.run_id,
                event_type="tool_call",
                name="read_source",
                started_at=now,
                ended_at=now,
                duration_ms=100,
            )
        )
    for index in range(retries):
        trace.add_event(
            AgentEvent(
                event_id=f"evt_{name}_retry_{index}",
                run_id=trace.run_id,
                event_type="retry",
                name="repair",
                started_at=now,
                ended_at=now,
                duration_ms=50,
            )
        )
    return trace


def test_ci_report_combines_replay_and_findings() -> None:
    baseline = _trace("baseline", duration_ms=1000, input_tokens=1000, retries=1)
    candidate = _trace("candidate", duration_ms=400, input_tokens=500)

    report = build_ci_report(
        baseline,
        candidate,
        gates=ReplayGates(min_latency_improvement_pct=20, min_cost_improvement_pct=20),
    )
    markdown = ci_report_to_markdown(report)

    assert report["passed"] is True
    assert report["summary"]["finding_count"] >= 1
    assert "# AgentLoop CI Report" in markdown
    assert "## Performance Gates" in markdown
    assert "## Top Findings" in markdown


def test_cli_ci_writes_markdown_and_json(tmp_path) -> None:
    baseline = _trace("baseline", duration_ms=1000, input_tokens=1000, retries=1)
    candidate = _trace("candidate", duration_ms=400, input_tokens=500)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline.export_json(baseline_path)
    candidate.export_json(candidate_path)
    out = tmp_path / "ci.md"
    json_out = tmp_path / "ci.json"

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
            "--min-latency-improvement-pct",
            "20",
            "--min-cost-improvement-pct",
            "20",
        ],
    )

    assert result.exit_code == 0
    assert out.exists()
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["summary"]["patchable_count"] >= 1


def test_cli_ci_exits_nonzero_when_gate_fails(tmp_path) -> None:
    baseline = _trace("baseline", duration_ms=300, input_tokens=200)
    candidate = _trace("candidate", duration_ms=800, input_tokens=200)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline.export_json(baseline_path)
    candidate.export_json(candidate_path)

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--max-latency-regression-pct",
            "0",
        ],
    )

    assert result.exit_code == 1


def test_cli_ci_fails_supplied_quality_fixtures_without_minimum_score(tmp_path) -> None:
    baseline = _trace("baseline", duration_ms=1000, input_tokens=1000)
    candidate = _trace("candidate", duration_ms=400, input_tokens=500)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    fixture_path = tmp_path / "fixtures.json"
    json_out = tmp_path / "ci.json"
    baseline.export_json(baseline_path)
    candidate.export_json(candidate_path)
    fixture_path.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "wrong-answer",
                        "baseline_output": "ok",
                        "candidate_output": "wrong",
                        "expected": "ok",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--quality-fixtures",
            str(fixture_path),
            "--json-out",
            str(json_out),
        ],
    )

    assert result.exit_code == 1
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["passed"] is False
    fixture_gate = next(
        item for item in report["replay"]["gates"]["results"] if item["name"] == "quality_fixtures"
    )
    assert fixture_gate["passed"] is False
