from __future__ import annotations

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized


def test_packaged_demos_write_expected_files(tmp_path) -> None:
    base = run_baseline(tmp_path)
    opt = run_optimized(tmp_path)
    graph = run_langgraph_style(tmp_path)
    assert base.name == "research_agent_baseline.json"
    assert opt.name == "research_agent_optimized.json"
    assert graph.name == "langgraph_style_demo.json"
    assert base.exists()
    assert opt.exists()
    assert graph.exists()


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
