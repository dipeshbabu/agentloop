from __future__ import annotations

import io
import json

import pytest
from test_real_agent_study import CSV

from agentloop.ablations import build_ablation_report
from examples.real_agent_study.run import file_hash
from examples.real_harness_study.protocol import load_plan, prepare
from examples.real_harness_study.runner import execute_observation


@pytest.fixture
def plan(tmp_path, monkeypatch):
    import examples.real_harness_study.protocol as protocol_module

    sources = tmp_path / "input"
    sources.mkdir()
    for name in ("winequality-red.csv", "winequality-white.csv"):
        (sources / name).write_text(CSV)
    hashes = {
        name: file_hash(sources / name) for name in ("winequality-red.csv", "winequality-white.csv")
    }
    monkeypatch.setattr(protocol_module, "DATA_HASHES", hashes)
    root = tmp_path / "study"
    return prepare(root, sources), root


def test_plan_counterbalances_every_condition_and_retains_missing_slots(plan):
    value, root = plan
    assert load_plan(root / "plan.json") == value
    report = build_ablation_report(value["protocol"], [])
    assert report["planned_observation_count"] == 108 and report["observed_count"] == 0
    specification = value["protocol"]["specification"]
    for split in ("pilot", "held_out"):
        slots = [
            slot
            for slot in specification["schedule"]
            if specification["tasks"][slot["task_id"]]["split"] == split
        ]
        for condition in specification["conditions"]:
            counts = [
                sum(slot["order"][position] == condition for slot in slots) for position in range(6)
            ]
            assert len(set(counts)) == 1


def test_empty_execution_exports_all_missing_slots(plan):
    from examples.real_harness_study.export import export

    value, root = plan
    report = export(root / "plan.json", root / "empty-report")
    assert report["planned_observation_count"] == 108 and report["observed_count"] == 0
    assert report["artifact_links_complete"] is False


@pytest.mark.parametrize(
    "condition,calls,success,status",
    [
        ("off", 2, True, "completed"),
        ("trace", 2, True, "completed"),
        ("shadow_1", 2, True, "completed"),
        ("enforce_1", 1, False, "stopped"),
        ("shadow_2", 2, True, "completed"),
        ("enforce_2", 2, True, "completed"),
    ],
)
def test_real_wrappers_preserve_shadow_and_deny_before_dispatch(
    plan, monkeypatch, condition, calls, success, status
):
    import examples.real_agent_study.api as api

    value, root = plan
    replies = iter(
        [
            '{"action":"sql","argument":"SELECT count(*) FROM wines WHERE color=\'white\' AND quality>=7"}',
            '{"action":"final","answer":{"rows":[[0]]}}',
        ]
    )
    dispatched = []

    def response(request, **kwargs):
        dispatched.append(request)
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [{"message": {"content": next(replies)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", response)
    task = value["tasks"][0]
    slot = next(
        slot
        for slot in value["protocol"]["specification"]["schedule"]
        if slot["task_id"] == task["id"]
    )
    receipt = execute_observation(
        value, task, slot, condition, slot["order"].index(condition), root, "http://127.0.0.1:8766"
    )
    observation = receipt["observation"]
    assert len(dispatched) == observation["metrics"]["model_calls"] == calls
    assert observation["success"] is success and observation["status"] == status
    assert observation["metrics"]["tokens"] == 15 * calls
    assert observation["metrics"]["cost_usd"] is None
    assert (observation["trace_run_id"] is None) == (condition == "off")
    if condition == "shadow_1":
        decisions = receipt["policy_evidence"]["decisions"].values()
        assert any(
            item["requested_action"] == "deny" and item["outcome"] == "proposed"
            for item in decisions
        )
    if condition == "enforce_1":
        assert observation["stop_reason"]


def test_changed_labels_rejected(plan):
    value, root = plan
    value["tasks"][0]["expected"] = [[999]]
    (root / "plan.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="label"):
        load_plan(root / "plan.json")


def test_cancellation_preserves_partial_evidence_without_zero_imputation(plan, monkeypatch):
    import examples.real_agent_study.api as api

    value, root = plan
    task = value["tasks"][0]
    slot = next(
        slot
        for slot in value["protocol"]["specification"]["schedule"]
        if slot["task_id"] == task["id"]
    )

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(api.urllib.request, "urlopen", cancel)
    with pytest.raises(KeyboardInterrupt):
        execute_observation(
            value, task, slot, "trace", slot["order"].index("trace"), root, "http://127.0.0.1:8766"
        )
    receipt = json.loads(next((root / "attempts").glob("*/receipt.json")).read_text())
    row = receipt["observation"]
    assert row["status"] == "cancelled" and row["quality_score"] is None
    assert row["metrics"]["tokens"] is row["metrics"]["tool_calls"] is None
    assert receipt["model_calls"][0]["status"] == "error"


def test_partial_export_preserves_denominator_and_original_observation(plan, monkeypatch):
    import examples.real_agent_study.api as api
    from examples.real_harness_study.export import export

    value, root = plan
    task = value["tasks"][0]
    slot = next(
        slot
        for slot in value["protocol"]["specification"]["schedule"]
        if slot["task_id"] == task["id"]
    )
    response = {
        "choices": [{"message": {"content": '{"action":"final","answer":{"rows":[[0]]}}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    monkeypatch.setattr(
        api.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(response).encode()),
    )
    original = execute_observation(
        value, task, slot, "trace", slot["order"].index("trace"), root, "http://127.0.0.1:8766"
    )
    report = export(root / "plan.json", root / "partial-report")
    assert report["planned_observation_count"] == 108 and report["observed_count"] == 1
    assert report["artifact_links_complete"] is False
    retained = json.loads((root / "partial-report/observations.json").read_text())
    assert retained == [original["observation"]]
