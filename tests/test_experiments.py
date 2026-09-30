from __future__ import annotations

import json
from asyncio import CancelledError
from dataclasses import replace

import pytest

from agentloop.budget_types import Reservation, ResourceUsage
from agentloop.entrypoint import _quickstart_trace
from agentloop.experiment_artifacts import journal_lock, read_json
from agentloop.experiment_reports import export_experiment, persist_experiment, summarize_experiment
from agentloop.experiment_types import (
    ExperimentBudget,
    ExperimentCase,
    ExperimentPlan,
    ExperimentResult,
    ExperimentRunner,
    fingerprint,
)
from agentloop.experiments import ExperimentSession
from agentloop.findings import build_diagnosis
from agentloop.replay import ReplayGates
from agentloop.store import SQLiteTraceStore


def case(identity="case"):
    baseline = _quickstart_trace()
    baseline.run_id = "baseline_" + identity
    for event in baseline.events:
        event.run_id = baseline.run_id
    target = build_diagnosis(baseline)["findings"][0]["finding_id"]
    return ExperimentCase(
        identity,
        baseline,
        target_finding_ids=[target],
        inputs={"value": 4},
        expected=4,
        baseline_output=4,
        input_ref="fixture:input",
        quality_ref="fixture:independent-answer",
        baseline_implementation_ref="fixture-baseline:v1",
        baseline_configuration_ref="fixture-config:v1",
    )


def runner(name="candidate", callback=None, *, reservation=None):
    return ExperimentRunner(
        name,
        "1",
        "fixture-implementation:v1",
        "fixture-config:v1",
        callback or (lambda request: ExperimentResult(request.inputs["value"])),
        reservation=reservation or Reservation(),
    )


def session(tmp_path, *, cases=None, runners=None, budget=None):
    cases, runners = cases or [case()], runners or [runner()]
    plan = ExperimentPlan(
        "fixture",
        cases=cases,
        runners=runners,
        intervention_type="offline-change",
        intervention_version="1",
        scorer={"type": "exact_match", "version": "1.0"},
        gate_version="1",
        gates=ReplayGates(min_quality_score=1),
        budget=budget or ExperimentBudget(10, 5),
        permission_ref="synthetic-unit-fixture",
    )
    return ExperimentSession(plan, cases=cases, runners=runners, root=tmp_path), cases, runners


def test_expected_effects_precede_results_and_native_evidence_is_reused(tmp_path):
    value, cases, runners = session(tmp_path)
    prediction = tmp_path / "predictions" / (fingerprint("case") + ".json")
    original = prediction.read_bytes()
    assert value.run()["executed"] == 0
    assert summarize_experiment(tmp_path)["status_counts"] == {"not_started": 1}
    assert value.run(enabled=True)["executed"] == 1
    report = summarize_experiment(tmp_path)
    assert report["comparisons"][0]["all_planned_gates_passed"]
    assert prediction.read_bytes() == original
    assert value.run(enabled=True)["executed"] == 0
    restored = ExperimentSession.resume(tmp_path, cases=cases, runners=runners)
    assert restored.run(enabled=True)["executed"] == 0
    exported = export_experiment(tmp_path)
    assert exported == export_experiment(tmp_path)
    assert exported["studies"]["candidate"]["conditions"]
    database = SQLiteTraceStore(str(tmp_path / "native.db"))
    first = persist_experiment(tmp_path, database)
    assert persist_experiment(tmp_path, database) == first and len(first) == 1


def test_quality_regression_retains_faster_candidate_as_rejected(tmp_path):
    value, _, _ = session(
        tmp_path, runners=[runner("good"), runner("bad", lambda request: ExperimentResult(5))]
    )
    value.run(enabled=True)
    comparisons = {
        item["candidate_id"]: item for item in summarize_experiment(tmp_path)["comparisons"]
    }
    assert comparisons["good"]["all_planned_gates_passed"]
    assert not comparisons["bad"]["all_planned_gates_passed"]
    assert (
        comparisons["bad"]["observed_pairs"] == 1 and comparisons["bad"]["quality_pass_count"] == 0
    )


@pytest.mark.parametrize("kind", ["failed", "timed_out", "cancelled", "invalid"])
def test_failed_timed_out_and_cancelled_attempts_are_retained(tmp_path, kind):
    error = (
        CancelledError("private")
        if kind == "cancelled"
        else TimeoutError("private")
        if kind == "timed_out"
        else RuntimeError("private")
    )

    def invoke(request):
        if kind == "invalid":
            return 4
        raise error

    value, _, _ = session(tmp_path, runners=[runner(callback=invoke)])
    if kind == "cancelled":
        with pytest.raises(CancelledError) as caught:
            value.run(enabled=True)
        assert caught.value is error
    else:
        value.run(enabled=True)
    report = summarize_experiment(tmp_path)
    assert report["status_counts"] == {"failed" if kind == "invalid" else kind: 1}
    assert not report["comparisons"][0]["all_planned_gates_passed"]
    assert "private" not in json.dumps(report)
    assert value.run(enabled=True)["executed"] == 0


def test_unmatched_and_unresolved_slots_are_not_fabricated_or_repeated(tmp_path):
    value, _, _ = session(tmp_path, cases=[case("a"), case("b")], budget=ExperimentBudget(1, 5))
    result = value.run(enabled=True)
    assert result["stop_reason"] == "execution_budget_exhausted"
    report = summarize_experiment(tmp_path)
    assert report["planned_attempts"] == 2 and report["observed_attempts"] == 1
    assert report["status_counts"] == {"completed": 1, "not_started": 1}
    assert not report["comparisons"][0]["all_planned_gates_passed"]
    assert export_experiment(tmp_path)["studies"]["candidate"]["conditions"]
    first = next((tmp_path / "attempts").glob("*/receipt.json"))
    first.unlink()  # Simulate loss before atomic final receipt publication.
    assert summarize_experiment(tmp_path)["status_counts"]["unresolved"] == 1
    with pytest.raises(ValueError, match="uncertain"):
        value.run(enabled=True)


def test_changed_predictions_and_bindings_block_execution(tmp_path):
    value, cases, runners = session(tmp_path)
    changed = replace(runners[0], configuration_ref="changed")
    with pytest.raises(ValueError, match="differs"):
        ExperimentSession.resume(tmp_path, cases=cases, runners=[changed])
    prediction = tmp_path / "predictions" / (fingerprint("case") + ".json")
    prediction.write_text("{}")
    with pytest.raises(ValueError, match="prediction"):
        value.run(enabled=True)


def test_resource_reservations_are_not_refunded_on_resume(tmp_path):
    reserve = Reservation(tokens=2, cost_usd=0.1, provenance="upper_bound", pricing_known=True)
    usage = ResourceUsage(
        tokens=1,
        cost_usd=0.01,
        token_provenance="user_supplied",
        cost_provenance="user_reported",
        complete=True,
    )
    value, cases, runners = session(
        tmp_path,
        cases=[case("a"), case("b")],
        runners=[
            runner(callback=lambda request: ExperimentResult(4, usage=usage), reservation=reserve)
        ],
        budget=ExperimentBudget(2, 5, max_tokens=2, max_cost_usd=0.1),
    )
    assert value.run(enabled=True)["executed"] == 1
    resumed = ExperimentSession.resume(tmp_path, cases=cases, runners=runners)
    assert resumed.run(enabled=True)["executed"] == 0
    assert summarize_experiment(tmp_path)["observed_attempts"] == 1


def test_ownership_lock_and_safe_filenames(tmp_path):
    value, _, _ = session(tmp_path, cases=[case("case:with:colons")])
    assert all(":" not in path.name for path in (tmp_path / "baselines").iterdir())
    with journal_lock(tmp_path), pytest.raises(ValueError, match="busy"):
        value.run(enabled=True)


def test_corrupted_result_is_not_recomputed_or_silently_dropped(tmp_path):
    value, _, _ = session(tmp_path)
    value.run(enabled=True)
    path = next((tmp_path / "attempts").glob("*/quality.json"))
    data = read_json(path)
    data["passed"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed"):
        summarize_experiment(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        value.run(enabled=True)


def test_late_result_is_failed_and_usage_is_retained(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("agentloop.experiments.monotonic", lambda: clock[0])
    monkeypatch.setattr("agentloop.experiment_types.monotonic", lambda: clock[0])

    def invoke(request):
        assert request.remaining_s == 5
        clock[0] = 6
        return ExperimentResult(
            4, usage=ResourceUsage(tokens=7, token_provenance="user_supplied", complete=True)
        )

    value, _, _ = session(tmp_path, runners=[runner(callback=invoke)])
    value.run(enabled=True)
    result = summarize_experiment(tmp_path)
    assert result["status_counts"] == {"timed_out": 1}
    assert result["attempts"][0]["receipt"]["usage"]["tokens"] == 7
    assert not result["comparisons"][0]["all_planned_gates_passed"]


def test_unknown_cost_with_limit_stops_future_candidates(tmp_path):
    reserve = Reservation(cost_usd=0, provenance="upper_bound", pricing_known=True)
    value, cases, runners = session(
        tmp_path,
        cases=[case("a"), case("b")],
        runners=[runner(reservation=reserve)],
        budget=ExperimentBudget(2, 5, max_cost_usd=0),
    )
    assert value.run(enabled=True)["stop_reason"] == "prior_budget_control"
    report = summarize_experiment(tmp_path)
    assert report["status_counts"] == {"not_started": 1, "stopped": 1}
    assert report["budget"]["unavailable_actual_cost_count"] == 1
    assert (
        ExperimentSession.resume(tmp_path, cases=cases, runners=runners).run(enabled=True)[
            "executed"
        ]
        == 0
    )


def test_report_does_not_rerun_implementation_or_scorer(tmp_path, monkeypatch):
    calls = []
    value, _, _ = session(
        tmp_path,
        runners=[runner(callback=lambda request: calls.append(True) or ExperimentResult(4))],
    )
    value.run(enabled=True)
    monkeypatch.setattr(
        "agentloop.experiments.build_quality_report",
        lambda *args, **kwargs: pytest.fail("rescored"),
    )
    assert summarize_experiment(tmp_path)["observed_attempts"] == 1
    export_experiment(tmp_path)
    assert value.run(enabled=True)["executed"] == 0 and calls == [True]


def test_cancellation_is_not_masked_by_receipt_write_failure(tmp_path, monkeypatch):
    from agentloop import experiments

    error = CancelledError("original")

    def invoke(request):
        raise error

    value, _, _ = session(tmp_path, runners=[runner(callback=invoke)])
    original = experiments.write_once

    def write(root, relative, data):
        if relative.endswith("receipt.json"):
            raise OSError("storage failed")
        return original(root, relative, data)

    monkeypatch.setattr(experiments, "write_once", write)
    with pytest.raises(CancelledError) as caught:
        value.run(enabled=True)
    assert caught.value is error
    assert summarize_experiment(tmp_path)["status_counts"] == {"unresolved": 1}


def test_multiple_finding_snapshots_keep_native_sorted_identity(tmp_path):
    baseline = _quickstart_trace()
    ids = [item["finding_id"] for item in build_diagnosis(baseline)["findings"]]
    selected = ExperimentCase(
        "multi",
        baseline,
        target_finding_ids=ids,
        inputs={},
        expected=4,
        baseline_output=4,
        input_ref="fixture",
        quality_ref="fixture",
        baseline_implementation_ref="fixture",
        baseline_configuration_ref="fixture",
    )
    value, _, _ = session(
        tmp_path, cases=[selected], runners=[runner(callback=lambda request: ExperimentResult(4))]
    )
    value.run(enabled=True)
    assert summarize_experiment(tmp_path)["observed_attempts"] == 1


def test_empty_export_keeps_planned_denominators_and_no_fake_study(tmp_path):
    session(tmp_path)
    exported = export_experiment(tmp_path)
    assert exported["experiment"]["planned_attempts"] == 1
    assert exported["experiment"]["observed_attempts"] == 0
    assert exported["studies"]["candidate"]["reason"] == "no_candidate_trace"


def test_artifact_path_cannot_escape_root(tmp_path):
    from agentloop.experiment_artifacts import local_path

    with pytest.raises(ValueError, match="escapes"):
        local_path(tmp_path, "../outside.json")


def test_unicode_evidence_uses_native_trace_and_quality_hashes(tmp_path):
    baseline = _quickstart_trace()
    baseline.name = "café 評価"
    baseline.metadata["output"] = "réponse"
    target = build_diagnosis(baseline)["findings"][0]["finding_id"]
    selected = ExperimentCase(
        "unicode",
        baseline,
        target_finding_ids=[target],
        inputs={"value": "réponse"},
        expected="réponse",
        baseline_output="réponse",
        input_ref="入力:v1",
        quality_ref="正解:v1",
        baseline_implementation_ref="baseline:v1",
        baseline_configuration_ref="config:v1",
    )
    value, _, _ = session(tmp_path, cases=[selected])
    value.run(enabled=True)
    assert export_experiment(tmp_path)["experiment"]["comparisons"][0]["all_planned_gates_passed"]


def test_persistence_rejects_conflicting_run_before_overwrite(tmp_path):
    value, cases, _ = session(tmp_path / "artifacts")
    value.run(enabled=True)
    store = SQLiteTraceStore(str(tmp_path / "native.db"))
    baseline = cases[0].payload()["baseline"]
    from agentloop.tracer import AgentTrace

    changed = AgentTrace.from_dict(baseline)
    changed.metadata["changed"] = True
    store.save_trace(changed)
    with pytest.raises(ValueError, match="conflicting"):
        persist_experiment(value.root, store)
    assert store.get_trace(changed.run_id).metadata["changed"] is True


def test_concurrent_execution_cannot_claim_duplicate_work(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def invoke(request):
        entered.set()
        assert release.wait(timeout=3)
        return ExperimentResult(4)

    value, _, _ = session(tmp_path, runners=[runner(callback=invoke)])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(value.run, enabled=True)
        try:
            assert entered.wait(timeout=3)
            with pytest.raises(ValueError, match="busy"):
                value.run(enabled=True)
        finally:
            release.set()
        assert future.result(timeout=3)["executed"] == 1
    assert summarize_experiment(tmp_path)["observed_attempts"] == 1


def test_missing_usage_stays_unknown_in_summary(tmp_path):
    value, _, _ = session(tmp_path)
    value.run(enabled=True)
    budget = summarize_experiment(tmp_path)["budget"]
    assert budget["reported_total_cost_usd"] is None
    assert budget["reported_total_tokens"] is None
    assert budget["unknown_cost_reservations"] == 1


def test_baseline_output_cannot_disagree_with_recorded_output():
    baseline = _quickstart_trace()
    baseline.metadata["output"] = 5
    target = build_diagnosis(baseline)["findings"][0]["finding_id"]
    with pytest.raises(ValueError, match="recorded workflow output"):
        ExperimentCase(
            "wrong",
            baseline,
            target_finding_ids=[target],
            inputs={},
            expected=4,
            baseline_output=4,
            input_ref="input",
            quality_ref="quality",
            baseline_implementation_ref="baseline",
            baseline_configuration_ref="config",
        )


def test_html_export_uses_readable_escaped_tables(tmp_path):
    from agentloop.experiment_reports import experiment_to_html

    value, _, _ = session(tmp_path)
    value.run(enabled=True)
    report = summarize_experiment(tmp_path)
    report["name"] = "<script>bad</script>"
    page = experiment_to_html(report)
    assert "<table>" in page and 'scope="col"' in page
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_candidate_admission_cannot_predate_frozen_predictions(tmp_path):
    value, _, _ = session(tmp_path)
    value.run(enabled=True)
    start = next((tmp_path / "attempts").glob("*/started.json"))
    receipt = start.with_name("receipt.json")
    admission, outcome = read_json(start), read_json(receipt)
    admission["started_at"] = "2000-01-01T00:00:00Z"
    outcome["started_hash"] = fingerprint(admission)
    start.write_text(json.dumps(admission))
    receipt.write_text(json.dumps(outcome))
    with pytest.raises(ValueError, match="precedes"):
        summarize_experiment(tmp_path)


def test_deadline_expired_during_admission_never_invokes_runner(tmp_path, monkeypatch):
    ticks = iter([0.0, 6.0])
    monkeypatch.setattr("agentloop.experiments.monotonic", lambda: next(ticks))
    monkeypatch.setattr("agentloop.experiment_types.monotonic", lambda: 6.0)
    value, _, _ = session(
        tmp_path, runners=[runner(callback=lambda request: pytest.fail("late callback"))]
    )
    value.run(enabled=True)
    report = summarize_experiment(tmp_path)
    assert report["status_counts"] == {"timed_out": 1}
    assert report["attempts"][0]["receipt"]["invoked"] is False


def test_receipt_cannot_hide_a_native_budget_stop(tmp_path):
    reserve = Reservation(cost_usd=0, provenance="upper_bound", pricing_known=True)
    value, _, _ = session(
        tmp_path,
        runners=[runner(reservation=reserve)],
        budget=ExperimentBudget(2, 5, max_cost_usd=0),
    )
    value.run(enabled=True)
    path = next((tmp_path / "attempts").glob("*/receipt.json"))
    receipt = read_json(path)
    assert receipt["halted"]
    receipt["halted"] = False
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="budget evidence"):
        value.run(enabled=True)
