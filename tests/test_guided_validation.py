from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop.improvement import finish
from agentloop.tracer import AgentTrace
from examples.guided_validation.agent import execute
from examples.guided_validation.run import run, tasks


@pytest.mark.parametrize("split", ["pilot", "held_out"])
def test_fixture_agents_use_tools_and_independent_answers(tmp_path, split):
    for task in tasks(split):
        assert "expected" not in task["input"]
        for condition, count in (("baseline", 3), ("candidate", 1), ("wrong", 1)):
            path = tmp_path / f"{task['id']}-{condition}.json"
            result = execute({"input": task["input"], "trace_path": str(path)}, condition=condition)
            trace = AgentTrace.from_json(path)
            assert result["error"] is None
            assert (result["output"] == task["expected"]) is (condition != "wrong")
            assert len(trace.events) == count
            assert trace.metadata["synthetic"] is True
    assert not {task["id"] for task in tasks("pilot")} & {task["id"] for task in tasks("held_out")}


def test_three_workloads_preserve_native_reports_and_negative_controls(tmp_path):
    root = tmp_path / "suite"
    summary = run(root, repetitions=1)
    assert summary["overhead"]["difference_ms"]["count"] == 6
    assert all(
        pair["both_correct"] and pair["matching_outputs"] for pair in summary["overhead"]["pairs"]
    )
    for comparison in summary["comparisons"]:
        report = json.loads((root / comparison["report"]).read_text())
        assert report["planned_attempts"] == (3 if comparison["split"] == "pilot" else 6)
        assert report["synthetic"] is True
        assert report["study"] is not None
        if comparison["condition"] == "wrong":
            assert report["passed_attempts"] == 0
            assert all(not case["quality"]["passed"] for case in report["cases"])
        else:
            assert all(case["quality"]["passed"] for case in report["cases"])
        assert all(
            case["intervention"] is None or case["intervention"]["gates_passed"] == case["passed"]
            for case in report["cases"]
        )
        # Measured timing governs acceptance; no fixed performance outcome is asserted.
    with pytest.raises(FileExistsError):
        run(root, repetitions=1)


def test_local_model_requires_identity_and_refuses_paid_endpoints(tmp_path):
    with pytest.raises(ValueError, match="model-file"):
        run(tmp_path / "missing", model_url="http://127.0.0.1:1234")
    with pytest.raises(ValueError, match="loopback"):
        run(
            tmp_path / "remote", model_url="https://provider.invalid", model_file=tmp_path / "model"
        )
    assert not (tmp_path / "remote").exists()


def test_real_langgraph_executes_the_same_tool_and_quality_contract(tmp_path):
    pytest.importorskip("langgraph.graph")
    task = tasks("pilot")[2]
    for condition, count in (("baseline", 3), ("candidate", 1)):
        path = tmp_path / (condition + ".json")
        result = execute(
            {"input": task["input"], "trace_path": str(path)},
            condition=condition,
            framework="langgraph",
        )
        assert result["output"] == task["expected"]
        assert len(AgentTrace.from_json(path).events) == count


def test_retained_local_model_evidence_recomputes_without_inference(tmp_path):
    from examples.real_agent_study.archive import unpack

    index = Path(__file__).resolve().parents[1] / "research/guided-2026-11/index.json"
    root = tmp_path / "evidence"
    assert unpack(index, root) == 300
    study = root / "study"
    report = finish(
        study / "held_out/session/session.json",
        study / "held_out/candidate/capture.json",
        tmp_path / "recomputed",
        change="Recompute retained evidence",
    )
    assert report["planned_attempts"] == 12 and report["passed_attempts"] == 3
    assert report["passed"] is False and report["synthetic"] is False
    assert all(case["replay"]["deltas"]["cost_usd_delta"] is None for case in report["cases"])
    summary = json.loads((study / "summary.json").read_text())
    assert summary["overhead"]["difference_ms"]["count"] == 0
    assert summary["overhead"]["difference_ms"]["missing_count"] == 6
