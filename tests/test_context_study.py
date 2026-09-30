from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from agentloop.events import utc_now_iso
from agentloop.tracer import record_model_call
from examples.context_study import protocol, runner
from examples.real_agent_study.run import file_hash


class FakeModel:
    invalid_summary = False
    wrong_answer = False

    def __init__(self, *args, **kwargs):
        self.receipts = []
        self.trace = None

    def complete(self, messages, *, remaining_s):
        if messages[0]["content"].startswith("Summarize"):
            output = {"summary": "x" * 1000 if self.invalid_summary else "A wine sample."}
        else:
            output = (
                {"rows": [[999]]}
                if self.wrong_answer
                else {"rows": json.loads(messages[-1]["content"])["rows"]}
            )
        self.receipts.append(
            {
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "output_text": json.dumps(output),
            }
        )
        if self.trace is not None:
            record_model_call(
                "fixture",
                trace=self.trace,
                duration_ms=0.01,
                started_at=utc_now_iso(),
                model="fixture-unpriced",
                input_tokens=10,
                output_tokens=5,
                token_provenance="user_supplied",
            )
        return json.dumps(output)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    sources = tmp_path / "inputs"
    sources.mkdir()
    header = (
        '"fixed acidity";"volatile acidity";"citric acid";"residual sugar";'
        '"chlorides";"free sulfur dioxide";"total sulfur dioxide";"density";'
        '"pH";"sulphates";"alcohol";"quality"\n'
    )
    # Synthetic, small database for offline runner tests only.
    csv = header + "7;0.2;0.3;12;0.05;20;60;0.99;3.2;0.5;11;7\n" * 24
    for name in protocol.DATA_HASHES:
        (sources / name).write_text(csv)
    monkeypatch.setattr(
        protocol, "DATA_HASHES", {name: file_hash(sources / name) for name in protocol.DATA_HASHES}
    )
    root = tmp_path / "study"
    return root, protocol.prepare(root, sources)


def test_frozen_inputs_and_balanced_heldout_condition_positions(plan):
    root, value = plan
    spec = value["protocol"]["specification"]
    slots = [s for s in spec["schedule"] if spec["tasks"][s["task_id"]]["split"] == "held_out"]
    for condition in protocol.CONDITIONS:
        assert sorted(slot["order"].index(condition) for slot in slots) == list(range(8))
    assert protocol.load_plan(root / "plan.json") == value
    value["tasks"][0]["expected"] = [[999]]
    (root / "plan.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen"):
        protocol.load_plan(root / "plan.json")


def test_native_report_preserves_summary_failures_and_missing_slots(plan, tmp_path):
    root, value = plan
    tasks = {task["id"]: task for task in value["tasks"]}
    slots = value["protocol"]["specification"]["schedule"]

    class BadSummary(FakeModel):
        invalid_summary = True

    for slot in (slots[0], slots[2]):
        for position, condition in enumerate(slot["order"]):
            receipt = runner.execute(
                value,
                tasks[slot["task_id"]],
                slot,
                condition,
                position,
                root,
                "http://127.0.0.1",
                model_factory=BadSummary,
            )
            row = receipt["observation"]
            if condition in {"enforce_context", "enforce_combined"}:
                assert row["status"] == "failed" and not row["success"]
                assert row["metrics"]["tokens"] == 15  # The failed summary still counts.
                record = next(iter(receipt["context_evidence"]["records"].values()))
                assert record["summary_invocations"] == 1 and not record["provider_invoked"]
            else:
                assert row["success"] and row["metrics"]["model_calls"] == 1
    report = runner.export(root / "plan.json", tmp_path / "export")
    assert report["planned_observation_count"] == 80 and report["observed_count"] == 16


def test_independent_quality_rejects_wrong_model_answer_and_shared_usage_counts_both_calls(plan):
    root, value = plan
    slot = value["protocol"]["specification"]["schedule"][0]
    task = value["tasks"][0]

    class WrongAnswer(FakeModel):
        wrong_answer = True

    condition = "enforce_combined"
    receipt = runner.execute(
        value,
        task,
        slot,
        condition,
        slot["order"].index(condition),
        root,
        "http://127.0.0.1",
        model_factory=WrongAnswer,
    )
    assert not receipt["quality"]["passed"] and receipt["observation"]["quality_score"] == 0
    assert receipt["observation"]["metrics"]["tokens"] == 30
    assert receipt["observation"]["metrics"]["model_calls"] == 2
    trace = json.loads(next((Path(root) / "attempts").glob("*/trace.json")).read_text())
    assert trace["metadata"]["agentloop.context_transform"] == receipt["context_evidence"]


def test_empty_or_interrupted_study_retains_every_planned_slot(plan, tmp_path):
    root, _ = plan
    report = runner.export(root / "plan.json", tmp_path / "empty-export")
    assert report["planned_observation_count"] == 80 and report["observed_count"] == 0
    assert not report["artifact_links_complete"]
    provenance = json.loads((tmp_path / "empty-export/provenance.json").read_text())
    assert len(provenance["unavailable"]) == 80


def test_cancelled_summary_preserves_partial_calls_without_complete_measurements(plan, tmp_path):
    root, value = plan
    slot = value["protocol"]["specification"]["schedule"][0]

    class Cancelled(FakeModel):
        def complete(self, messages, *, remaining_s):
            super().complete(messages, remaining_s=remaining_s)
            raise KeyboardInterrupt()

    condition = "enforce_context"
    with pytest.raises(KeyboardInterrupt):
        runner.execute(
            value,
            value["tasks"][0],
            slot,
            condition,
            slot["order"].index(condition),
            root,
            "http://127.0.0.1",
            model_factory=Cancelled,
        )
    receipt = json.loads(next((root / "attempts").glob("*/receipt.json")).read_text())
    row = receipt["observation"]
    assert row["status"] == "cancelled" and row["success"] is None
    assert row["quality_score"] is None and all(value is None for value in row["metrics"].values())
    assert len(receipt["model_calls"]) == 1
    assert runner.export(root / "plan.json", tmp_path / "cancelled-export")["observed_count"] == 1


@pytest.mark.parametrize("field", ["position", "tokens", "success", "context", "extra_attempt"])
def test_export_rejects_changed_or_unplanned_evidence(plan, tmp_path, field):
    root, value = plan
    slot = value["protocol"]["specification"]["schedule"][0]
    condition = "enforce_context"
    runner.execute(
        value,
        value["tasks"][0],
        slot,
        condition,
        slot["order"].index(condition),
        root,
        "http://127.0.0.1",
        model_factory=FakeModel,
    )
    path = next((root / "attempts").glob("*/receipt.json"))
    receipt = json.loads(path.read_text())
    if field == "position":
        receipt["observation"]["position"] += 1
    elif field == "tokens":
        receipt["observation"]["metrics"]["tokens"] = 1
    elif field == "success":
        receipt["observation"]["success"] = False
    elif field == "context":
        receipt["context_evidence"]["records"].clear()
    else:
        (root / "attempts/unplanned").mkdir()
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        runner.export(root / "plan.json", tmp_path / "invalid-export")


def test_published_evidence_retains_all_observations_and_resource_regression():
    from agentloop.ablations import build_ablation_report
    from examples.real_agent_study.results import read_bundle

    archive_root = Path(__file__).resolve().parents[1] / "research/context-transform-2026-09"
    index = json.loads((archive_root / "index.json").read_text())
    receipts, frozen, saved, count = [], None, None, 0
    for part in index["parts"]:
        path = archive_root / part["file"]
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
                    saved = json.loads(archive.read(name))
    assert count == index["total_files"] == 370
    assert len(receipts) == 80
    assert sum(len(row["model_calls"]) for row in receipts) == 100
    assert all(row["observation"]["success"] for row in receipts)
    assert all(row["observation"]["metrics"]["cost_usd"] is None for row in receipts)
    report = build_ablation_report(frozen["protocol"], [row["observation"] for row in receipts])
    assert report["comparisons"] == saved["comparisons"]
    comparison = next(
        row
        for row in report["comparisons"]
        if row["baseline"] == "trace"
        and row["candidate"] == "enforce_context"
        and row["split"] == "held_out"
    )
    assert comparison["quality_gate_counts"]["accepted"] == 8
    assert comparison["descriptive_deltas"]["tokens"]["task_summary"]["mean"] == 262
    assert comparison["descriptive_deltas"]["latency_ms"]["task_summary"]["mean"] > 0
