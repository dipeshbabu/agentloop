from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.context_types import ContextTokenCount
from agentloop.events import utc_now_iso
from agentloop.routing_types import ModelBackend, ModelCapabilities, RouteResponseInfo
from agentloop.tracer import record_model_call
from examples.real_agent_study.run import file_hash, fingerprint
from examples.routing_study import protocol, runner
from examples.routing_study.export import export
from examples.routing_study.runtime import LocalBackend, identity


@pytest.fixture
def plan(tmp_path, monkeypatch):
    data = tmp_path / "data.jsonl"
    data.write_text(
        "\n".join(
            json.dumps(
                {
                    "question": f"Synthetic arithmetic fixture {index}: what is 2+2?",
                    "answer": "#### 4",
                }
            )
            for index in range(12)
        )
    )
    monkeypatch.setattr(protocol, "DATA_SHA", file_hash(data))
    previous = []
    for index in range(2):
        path = tmp_path / f"prior-{index}.json"
        path.write_text(json.dumps({"tasks": [{"source_index": index}]}))
        previous.append(path)
    root = tmp_path / "study"
    return root, protocol.prepare(root, data, *previous)


class FakeClient:
    def __init__(self, base_url, condition, trace):
        self.condition, self.trace = condition, trace
        self.calls, self.tokenization = [], []

    def invoke(self, payload):
        assert payload["model"] == identity(self.condition).model
        text = json.dumps({"answer": "4" if self.condition == "baseline" else "5"})
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        record_model_call(
            "fixture",
            trace=self.trace,
            started_at=utc_now_iso(),
            # This fake does no provider work. Inventing 1 ms can exceed the
            # entire host decision duration on a fast CI runner.
            duration_ms=0.0,
            model=identity(self.condition).model,
            input_tokens=10,
            output_tokens=5,
            token_provenance="user_supplied",
            output_text=text,
        )
        self.calls.append(
            {
                "usage": usage,
                "output_text": text,
                "status": "completed",
                "token_count_matches_provider": True,
            }
        )
        return {
            "model": "fixture-reported",
            "usage": usage,
            "choices": [{"message": {"content": text}}],
        }

    def backend(self, condition):
        return ModelBackend(
            identity(condition),
            ModelCapabilities(
                "offline-fixture-v1",
                4096,
                parameters=(
                    "model",
                    "messages",
                    "max_tokens",
                    "temperature",
                    "seed",
                    "cache_prompt",
                    "response_format",
                ),
                output_modes=("json_object",),
            ),
            self.invoke,
            lambda payload: ContextTokenCount(10, "user_supplied", "offline-fixture-v1"),
            lambda value: RouteResponseInfo(
                ResourceUsage(tokens=15, token_provenance="user_supplied", complete=True),
                value["model"],
            ),
            dispatch=DispatchOptions(Reservation(tokens=4160, provenance="upper_bound")),
        )


def test_frozen_plan_excludes_old_tasks_and_rejects_label_mutation(plan):
    root, value = plan
    assert len(value["tasks"]) == 8
    assert not {0, 1}.intersection(task["source_index"] for task in value["tasks"])
    assert protocol.load_plan(root / "plan.json") == value
    value["tasks"][0]["expected"] = "wrong"
    (root / "plan.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen"):
        protocol.load_plan(root / "plan.json")


def test_controlled_route_keeps_bad_quality_and_original_prediction(plan):
    root, value = plan
    task = value["tasks"][0]
    baseline = runner.execute(
        value, task, "baseline", 0, root, "http://127.0.0.1", client_factory=FakeClient
    )
    before = root / "attempts/baseline/0" / task["id"]
    prediction = (before / "prediction.json").read_bytes()
    candidate = runner.execute(
        value, task, "candidate", 0, root, "http://127.0.0.1", client_factory=FakeClient
    )
    assert baseline["quality_score"] == 1 and candidate["quality_score"] == 0
    assert (before / "prediction.json").read_bytes() == prediction
    record = json.loads(
        (root / "attempts/candidate/0" / task["id"] / "intervention.json").read_text()
    )
    assert not record["gates_passed"]
    route = next(iter(candidate["routing"]["records"].values()))
    assert route["attempts"][0]["selected"] == identity("candidate").to_dict()
    assert candidate["operating_cost_usd"] is None
    with pytest.raises(FileExistsError):
        runner.execute(
            value, task, "baseline", 0, root, "http://127.0.0.1", client_factory=FakeClient
        )


def test_cancelled_attempt_is_retained_before_interrupt_propagates(plan):
    root, value = plan

    class Cancelled(FakeClient):
        def invoke(self, payload):
            raise KeyboardInterrupt()

    task = value["tasks"][0]
    with pytest.raises(KeyboardInterrupt):
        runner.execute(
            value, task, "baseline", 0, root, "http://127.0.0.1", client_factory=Cancelled
        )
    receipt = json.loads((root / "attempts/baseline/0" / task["id"] / "receipt.json").read_text())
    assert receipt["status"] == "cancelled" and receipt["quality_score"] is None
    assert receipt["decision_latency_ms"] is None


def test_real_adapter_retains_tokenizer_mismatch_as_failed_but_billed_usage(monkeypatch):
    from agentloop.routing_types import RouteProviderError

    client = LocalBackend("http://127.0.0.1:8766", "baseline")
    payload = {
        "model": identity("baseline").model,
        "messages": [{"role": "user", "content": "fixture"}],
    }
    client.tokenization.append({"request_hash": fingerprint(payload), "count": {"value": 10}})
    monkeypatch.setattr(
        client,
        "_post",
        lambda endpoint, data: {
            "model": "fixture",
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
            "choices": [{"message": {"content": "{}"}}],
        },
    )
    with pytest.raises(RouteProviderError):
        client.invoke(payload)
    assert client.calls[0]["usage"]["prompt_tokens"] == 11
    assert client.calls[0]["token_count_matches_provider"] is False
    assert client.calls[0]["status"] == "failed"


@pytest.mark.parametrize("decision_seconds", [0.0, 0.0005])
def test_empty_and_partial_exports_keep_the_full_planned_denominator(
    plan, tmp_path, monkeypatch, decision_seconds
):
    root, value = plan
    empty = export(root / "plan.json", tmp_path / "empty")
    assert empty["planned_trials"] == 32 and empty["observed_trials"] == 0
    assert empty["held_out_pairs"] == 12 and not empty["all_candidate_tasks_correct"]
    ticks = iter([0.0, decision_seconds, 0.0, decision_seconds])
    monkeypatch.setattr(runner, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    task = value["tasks"][2]
    runner.execute(value, task, "baseline", 0, root, "http://127.0.0.1", client_factory=FakeClient)
    runner.execute(value, task, "candidate", 0, root, "http://127.0.0.1", client_factory=FakeClient)
    partial = export(root / "plan.json", tmp_path / "partial")
    assert partial["planned_trials"] == 32 and partial["observed_trials"] == 2
    assert partial["quality_losses"] == 1
    assert partial["latency"]["planned_observation_count"] == 12


def test_export_rejects_corrupted_quality_and_unplanned_attempts(plan, tmp_path):
    root, value = plan
    task = value["tasks"][0]
    runner.execute(value, task, "baseline", 0, root, "http://127.0.0.1", client_factory=FakeClient)
    path = root / "attempts/baseline/0" / task["id"] / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["quality_score"] = 0
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="quality"):
        export(root / "plan.json", tmp_path / "bad-grade")
    (root / "attempts/unplanned").mkdir()
    with pytest.raises(ValueError, match="unplanned"):
        export(root / "plan.json", tmp_path / "bad-attempt")


def test_published_study_preserves_quality_losses_and_all_failed_outputs():
    from examples.real_agent_study.results import read_bundle

    root = Path(__file__).resolve().parents[1] / "research/model-routing-2026-09"
    index = json.loads((root / "index.json").read_text())
    receipts, summary, files, interventions = [], None, 0, 0
    for part in index["parts"]:
        path = root / part["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
        manifest = read_bundle(path)
        files += len(manifest["files"])
        with zipfile.ZipFile(path) as archive:
            for name in manifest["files"]:
                if name.startswith("study/attempts/") and name.endswith("/receipt.json"):
                    receipts.append(json.loads(archive.read(name)))
                elif name == "study/report/summary.json":
                    summary = json.loads(archive.read(name))
                elif name.startswith("study/attempts/") and name.endswith("/intervention.json"):
                    record = json.loads(archive.read(name))
                    assert len(record["predicted"]["findings"]) == 1
                    assert not record["gates_passed"]
                    interventions += 1
    assert files == index["total_files"] == 301 and len(receipts) == 32
    assert interventions == 16
    assert sum(row["status"] == "failed" for row in receipts) == 14
    assert all(call["token_count_matches_provider"] for row in receipts for call in row["calls"])
    assert summary["quality_losses"] == 6 and not summary["all_candidate_tasks_correct"]
    assert summary["both_correct_latency"]["observation_summary"]["count"] == 0
