from __future__ import annotations

import json

from agentloop.tracer import AgentTrace
from examples import research_experiment_demo


def test_research_experiment_demo_is_offline_and_reproducible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(research_experiment_demo, "RUNS", tmp_path)

    research_experiment_demo.main()

    baseline = AgentTrace.from_json(tmp_path / "baseline.json")
    candidate = AgentTrace.from_json(tmp_path / "candidate.json")
    comparison = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))

    assert baseline.metadata["experiment_id"] == "parallel-retrieval-v1"
    assert baseline.metadata["condition"] == "sequential"
    assert candidate.metadata["condition"] == "parallel"
    assert baseline.metadata["task_id"] == candidate.metadata["task_id"] == "task-001"
    assert baseline.metadata["seed"] == candidate.metadata["seed"] == 0
    assert comparison["deltas"]["latency_improvement_pct"] > 0
    assert comparison["gates"]["passed"] is True
    assert "summary" in comparison
