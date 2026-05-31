from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.events import AgentEvent, utc_now_iso
from agentloop.replay import ReplayGates, build_replay_report, replay_report_to_markdown
from agentloop.tracer import AgentTrace


def _trace(
    name: str,
    *,
    model_duration_ms: float,
    tool_duration_ms: float,
    input_tokens: int,
    output_tokens: int,
    retries: int = 0,
) -> AgentTrace:
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
            duration_ms=model_duration_ms,
            model="gpt-4.1-mini",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    trace.add_event(
        AgentEvent(
            event_id=f"evt_{name}_tool",
            run_id=trace.run_id,
            event_type="tool_call",
            name="search",
            started_at=now,
            ended_at=now,
            duration_ms=tool_duration_ms,
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


def test_replay_report_passes_for_lower_cost_latency_and_retries() -> None:
    baseline = _trace("baseline", model_duration_ms=800, tool_duration_ms=200, input_tokens=1000, output_tokens=200, retries=1)
    candidate = _trace("candidate", model_duration_ms=400, tool_duration_ms=100, input_tokens=500, output_tokens=100, retries=0)

    report = build_replay_report(
        baseline,
        candidate,
        gates=ReplayGates(min_latency_improvement_pct=40, min_cost_improvement_pct=40),
    )

    assert report["gates"]["passed"] is True
    assert report["deltas"]["latency_improvement_pct"] >= 40
    assert report["deltas"]["cost_improvement_pct"] >= 40
    assert report["deltas"]["retry_count_delta"] == -1
    assert "Replay passed" in report["summary"]


def test_replay_report_fails_on_latency_regression() -> None:
    baseline = _trace("baseline", model_duration_ms=500, tool_duration_ms=100, input_tokens=200, output_tokens=50)
    candidate = _trace("candidate", model_duration_ms=800, tool_duration_ms=200, input_tokens=200, output_tokens=50)

    report = build_replay_report(baseline, candidate, gates=ReplayGates(max_latency_regression_pct=10))

    assert report["gates"]["passed"] is False
    latency_gate = next(item for item in report["gates"]["results"] if item["name"] == "latency_regression")
    assert latency_gate["passed"] is False


def test_replay_report_checks_schema_and_quality_gates() -> None:
    baseline = _trace("baseline", model_duration_ms=800, tool_duration_ms=200, input_tokens=1000, output_tokens=200)
    candidate = _trace("candidate", model_duration_ms=400, tool_duration_ms=100, input_tokens=500, output_tokens=100)
    baseline.metadata.update({"schema_validity_pct": 100.0, "quality_score": 0.91})
    candidate.metadata.update({"schema_valid": True, "schema_validity_pct": 100.0, "quality_score": 0.94})

    report = build_replay_report(
        baseline,
        candidate,
        gates=ReplayGates(require_schema_valid=True, min_quality_score=0.9),
    )
    markdown = replay_report_to_markdown(report)

    assert report["gates"]["passed"] is True
    assert report["candidate"]["schema_valid"] is True
    assert report["candidate"]["quality_score"] == 0.94
    assert "schema_valid" in {item["name"] for item in report["gates"]["results"]}
    assert "Quality score" in markdown


def test_replay_report_uses_fixture_quality_report() -> None:
    baseline = _trace("baseline", model_duration_ms=800, tool_duration_ms=200, input_tokens=1000, output_tokens=200)
    candidate = _trace("candidate", model_duration_ms=400, tool_duration_ms=100, input_tokens=500, output_tokens=100)
    quality = {
        "case_count": 1,
        "passed": True,
        "baseline_score": 0.8,
        "candidate_score": 0.95,
        "quality_delta": 0.15,
        "failed_case_count": 0,
        "failed_cases": [],
        "cases": [],
    }

    report = build_replay_report(
        baseline,
        candidate,
        gates=ReplayGates(min_quality_score=0.9),
        quality_report=quality,
    )

    assert report["gates"]["passed"] is True
    assert report["candidate"]["quality_score"] == 0.95
    assert report["quality"]["candidate_score"] == 0.95


def test_replay_markdown_contains_gate_table() -> None:
    baseline = _trace("baseline", model_duration_ms=500, tool_duration_ms=100, input_tokens=200, output_tokens=50)
    candidate = _trace("candidate", model_duration_ms=400, tool_duration_ms=100, input_tokens=100, output_tokens=50)

    markdown = replay_report_to_markdown(build_replay_report(baseline, candidate))

    assert "# AgentLoop Replay Report" in markdown
    assert "| Gate | Status | Detail |" in markdown
    assert "latency_regression" in markdown


def test_cli_replay_writes_markdown_and_json(tmp_path) -> None:
    baseline = _trace("baseline", model_duration_ms=800, tool_duration_ms=200, input_tokens=1000, output_tokens=200, retries=1)
    candidate = _trace("candidate", model_duration_ms=400, tool_duration_ms=100, input_tokens=500, output_tokens=100)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline.export_json(baseline_path)
    candidate.export_json(candidate_path)
    out = tmp_path / "replay.md"
    json_out = tmp_path / "replay.json"

    result = CliRunner().invoke(
        app,
        [
            "replay",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
            "--min-latency-improvement-pct",
            "40",
            "--min-cost-improvement-pct",
            "40",
        ],
    )

    assert result.exit_code == 0
    assert out.exists()
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["gates"]["passed"] is True
    assert payload["baseline"]["name"] == "baseline"


def test_cli_replay_exits_nonzero_when_gate_fails(tmp_path) -> None:
    baseline = _trace("baseline", model_duration_ms=500, tool_duration_ms=100, input_tokens=200, output_tokens=50)
    candidate = _trace("candidate", model_duration_ms=800, tool_duration_ms=200, input_tokens=200, output_tokens=50)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline.export_json(baseline_path)
    candidate.export_json(candidate_path)

    result = CliRunner().invoke(
        app,
        ["replay", "--baseline", str(baseline_path), "--candidate", str(candidate_path), "--max-latency-regression-pct", "0"],
    )

    assert result.exit_code == 1
