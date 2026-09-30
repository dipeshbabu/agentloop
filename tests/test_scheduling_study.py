from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from agentloop.graph import ExecutionGraph
from agentloop.tracer import AgentTrace
from examples.real_agent_study.run import file_hash
from examples.scheduling_study import protocol, runner
from examples.scheduling_study.export import export


@pytest.fixture
def plan(tmp_path, monkeypatch):
    inputs = tmp_path / "sources"
    inputs.mkdir()
    header = '"fixed acidity";"volatile acidity";"citric acid";"residual sugar";"chlorides";"free sulfur dioxide";"total sulfur dioxide";"density";"pH";"sulphates";"alcohol";"quality"\n'
    text = header + "7;0.2;0.3;12;0.05;20;60;0.99;3.2;0.5;11;7\n" * 24
    for name in protocol.DATA_HASHES:
        (inputs / name).write_text(text)
    monkeypatch.setattr(
        protocol, "DATA_HASHES", {name: file_hash(inputs / name) for name in protocol.DATA_HASHES}
    )
    root = tmp_path / "study"
    return root, protocol.prepare(root, inputs)


def test_all_conditions_preserve_ordered_outputs_and_database(plan):
    root, value = plan
    original = file_hash(root / "wine.sqlite")
    slot = value["protocol"]["specification"]["schedule"][0]
    task = value["tasks"][0]
    for position, condition in enumerate(slot["order"]):
        receipt = runner.execute(value, task, slot, condition, position, root)
        assert receipt["quality"]["passed"] and receipt["results"] == task["expected"]
        assert receipt["observation"]["metrics"]["tool_calls"] == 4
        assert receipt["observation"]["metrics"]["model_calls"] == 0
        assert receipt["observation"]["metrics"]["cost_usd"] is None
        if condition.endswith(("schedule", "combined")):
            record = next(iter(receipt["schedule_evidence"]["records"].values()))
            assert record["actual_concurrency_limit"] == (
                4 if condition.startswith("enforce") else 1
            )
            assert record["plan"]["unknown_safety"] == []
    assert file_hash(root / "wine.sqlite") == original
    for path in (root / "attempts").glob("*/trace.json"):
        trace = AgentTrace.from_json(path)
        graph = ExecutionGraph.from_trace(trace)
        assert graph.dependency_summary()["valid"]
        assert graph.dependency_summary()["declared_span_count"] == 4


def test_source_connection_rejects_writes_and_external_database_attachment(plan, tmp_path):
    root, _ = plan
    for sql in (
        "DELETE FROM wines",
        "CREATE TABLE other (value TEXT)",
        "ATTACH DATABASE ? AS extra",
    ):
        parameters = [str(tmp_path / "forbidden.sqlite")] if "ATTACH" in sql else []
        with pytest.raises(sqlite3.Error):
            protocol.read_query(root / "wine.sqlite", {"sql": sql, "parameters": parameters})
    assert not (tmp_path / "forbidden.sqlite").exists()


def test_task_quality_does_not_accept_wrong_values_or_boolean_numeric_coercion(plan):
    _, value = plan
    task = value["tasks"][0]
    wrong = json.loads(json.dumps(task["expected"]))
    wrong[0]["rows"] = [[True]]
    assert not runner.grade(wrong, task)["passed"]


def test_failed_query_retains_partial_results_and_cannot_win_by_doing_less(plan):
    root, value = plan
    task = value["tasks"][0]
    slot = value["protocol"]["specification"]["schedule"][0]

    def failed(path, query):
        if query["id"] == "white-alcohol":
            raise ValueError("PRIVATE failure")
        return protocol.read_query(path, query)

    receipt = runner.execute(
        value, task, slot, "trace", slot["order"].index("trace"), root, query_runner=failed
    )
    assert receipt["observation"]["status"] == "failed" and not receipt["observation"]["success"]
    assert [item["status"] for item in receipt["outcomes"]] == [
        "completed",
        "failed",
        "not_started",
        "not_started",
    ]
    assert len(receipt["results"]) == 1 and len(receipt["tool_receipts"]) == 2
    assert "PRIVATE" not in json.dumps(receipt)


def test_empty_export_and_partial_failed_case_keep_planned_slots(plan, tmp_path):
    root, value = plan
    empty = export(root / "plan.json", tmp_path / "empty")
    assert empty["planned_observation_count"] == 96 and empty["observed_count"] == 0
    task = value["tasks"][0]
    slot = value["protocol"]["specification"]["schedule"][0]

    def wrong(path, query):
        return [["incorrect"]]

    for condition in ("trace", "enforce_schedule"):
        runner.execute(
            value, task, slot, condition, slot["order"].index(condition), root, query_runner=wrong
        )
    partial = export(root / "plan.json", tmp_path / "partial")
    assert partial["planned_observation_count"] == 96 and partial["observed_count"] == 2
    assert all(not row["observation"]["success"] for row in partial["observations"])


def test_export_rejects_changed_schedule_receipt_or_unplanned_attempt(plan, tmp_path):
    root, value = plan
    slot = value["protocol"]["specification"]["schedule"][0]
    condition = "enforce_schedule"
    runner.execute(value, value["tasks"][0], slot, condition, slot["order"].index(condition), root)
    path = next((root / "attempts").glob("*/receipt.json"))
    receipt = json.loads(path.read_text())
    record = next(iter(receipt["schedule_evidence"]["records"].values()))
    record["peak_running"] = 100
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="schedule"):
        export(root / "plan.json", tmp_path / "bad-schedule")
    (root / "attempts/unplanned").mkdir()
    with pytest.raises(ValueError, match="unplanned"):
        export(root / "plan.json", tmp_path / "bad-attempt")


def test_published_scheduling_evidence_retains_overhead_and_ordered_results():
    from agentloop.ablations import build_ablation_report
    from examples.real_agent_study.results import read_bundle

    root = Path(__file__).resolve().parents[1] / "research/tool-scheduling-2026-09"
    index = json.loads((root / "index.json").read_text())
    receipts, frozen, original, count = [], None, None, 0
    for part in index["parts"]:
        path = root / part["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
        manifest = read_bundle(path)
        count += len(manifest["files"])
        with zipfile.ZipFile(path) as archive:
            for name in manifest["files"]:
                if name.startswith("study/attempts/") and name.endswith("/receipt.json"):
                    receipts.append(json.loads(archive.read(name)))
                elif name == "study/plan.json":
                    frozen = json.loads(archive.read(name))
                elif name == "study/report/report.json":
                    original = json.loads(archive.read(name))
    assert count == index["total_files"] == 416
    assert len(receipts) == 96 and sum(len(row["tool_receipts"]) for row in receipts) == 384
    assert all(row["observation"]["success"] for row in receipts)
    report = build_ablation_report(frozen["protocol"], [row["observation"] for row in receipts])
    assert report["comparisons"] == original["comparisons"]
    effect = next(
        row
        for row in report["comparisons"]
        if row["baseline"] == "trace"
        and row["candidate"] == "enforce_schedule"
        and row["split"] == "held_out"
    )
    assert effect["quality_gate_counts"]["accepted"] == 8
    assert effect["descriptive_deltas"]["latency_ms"]["task_summary"]["mean"] > 0
