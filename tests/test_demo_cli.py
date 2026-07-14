from __future__ import annotations

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair
from agentloop.findings import build_diagnosis
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.tracer import AgentTrace


def test_packaged_demos_write_expected_files(tmp_path) -> None:
    base = run_baseline(tmp_path)
    opt = run_optimized(tmp_path)
    graph = run_langgraph_style(tmp_path)
    proof_base, proof_candidate = run_proof_pair(tmp_path)
    assert base.name == "research_agent_baseline.json"
    assert opt.name == "research_agent_optimized.json"
    assert graph.name == "langgraph_style_demo.json"
    assert proof_base.name == "agentloop_proof_baseline.json"
    assert proof_candidate.name == "agentloop_proof_candidate.json"
    assert base.exists()
    assert opt.exists()
    assert graph.exists()
    assert proof_base.exists()
    assert proof_candidate.exists()


def test_proof_demo_exercises_closed_loop_findings(tmp_path) -> None:
    proof_base, proof_candidate = run_proof_pair(tmp_path)
    baseline = AgentTrace.from_json(proof_base)
    candidate = AgentTrace.from_json(proof_candidate)
    finding_types = {finding["type"] for finding in build_diagnosis(baseline)["findings"]}
    replay = build_replay_report(
        baseline,
        candidate,
        gates=ReplayGates(
            min_latency_improvement_pct=20,
            min_cost_improvement_pct=5,
            require_schema_valid=True,
            min_quality_score=0.9,
        ),
    )

    assert {
        "parallelize_tools",
        "cache_context",
        "add_schema_validation",
        "runaway_loop",
        "tool_oscillation",
    } <= finding_types
    assert replay["gates"]["passed"] is True


def test_cli_compare_autogenerates_missing_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["compare"])
    assert result.exit_code == 0
    assert (tmp_path / "runs" / "research_agent_baseline.json").exists()
    assert (tmp_path / "runs" / "research_agent_optimized.json").exists()


def test_cli_demo_all(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["demo-all"])
    assert result.exit_code == 0
    assert (tmp_path / "runs" / "research_agent_baseline.json").exists()
    assert (tmp_path / "runs" / "agentloop_proof_baseline.json").exists()
    assert (tmp_path / "runs" / "agentloop_proof_candidate.json").exists()
