"""Numeric unit fixtures simulate declared inputs; they are not empirical study results."""

from __future__ import annotations

import copy
import json
import runpy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.calibration import (
    CalibrationValidationError,
    calibration_to_markdown,
    summarize_calibration,
)
from agentloop.cli import app
from agentloop.events import AgentEvent
from agentloop.interventions import build_intervention
from agentloop.tracer import AgentTrace


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def make_case(
    directory,
    name,
    *,
    predicted=40,
    realized=20,
    split="fit",
    synthetic=False,
    task=None,
    repetition="0",
    status="completed",
    metric="latency",
    token_provenance="user_supplied",
    version="historic-v1",
    cost_basis="provider_reported",
    success=True,
    score=1,
):
    task = name if task is None else task
    traces = []
    for side, duration, day in (
        ("baseline", 100, 1),
        ("candidate", 100 - realized if metric == "latency" else 80, 3),
    ):
        start = datetime(2026, 1, day, tzinfo=timezone.utc)
        end = start + timedelta(milliseconds=duration)
        trace = AgentTrace(
            name=name,
            run_id=f"{name}-{side}",
            started_at=start.isoformat(),
            ended_at=end.isoformat(),
            elapsed_ms=duration,
            metadata={
                "task_id": task,
                "repetition": repetition,
                "synthetic": synthetic,
                "success": True if side == "baseline" else success,
                "quality_score": 1 if side == "baseline" else score,
            },
        )
        reported_cost = 1.0 if side == "baseline" else 1.0 - realized if metric == "cost" else 1.0
        trace.add_event(
            AgentEvent(
                event_id=f"{name}-{side}-span",
                run_id=trace.run_id,
                event_type="model_call",
                name="fixture-model",
                started_at=start.isoformat(),
                ended_at=end.isoformat(),
                duration_ms=duration,
                model="gpt-4o-mini",
                input_tokens=100,
                output_tokens=20,
                token_provenance=token_provenance,
                status="error" if side == "candidate" and status != "completed" else "ok",
                metadata={"provider_reported_cost_usd": reported_cost}
                if cost_basis == "provider_reported"
                else {},
            )
        )
        trace.export_json(directory / f"{name}-{side}.json")
        traces.append(trace)
    estimate = {
        "estimator_id": "historical-fixture",
        "estimator_version": version,
        "method": "heuristic",
        "formula": "frozen_signal",
        "parameters": {"weight": 1},
        "inputs": {
            "signal": predicted,
            "cost_status": "complete",
            "token_status": "exact" if token_provenance == "user_supplied" else "unavailable",
        },
        "unmodeled_metrics": ["cost" if metric == "latency" else "latency"],
    }
    finding = {
        "finding_id": f"finding-{name}",
        "estimate": estimate,
        "savings": {
            "estimated_latency_savings_ms": predicted if metric == "latency" else 0,
            "estimated_cost_savings_usd": predicted if metric == "cost" else 0,
        },
    }
    record = build_intervention(
        *traces,
        target_finding_ids=[finding["finding_id"]],
        intervention_type="fixture-change",
        diagnosis={"run_id": traces[0].run_id, "findings": [finding]},
        metadata={"synthetic": synthetic},
    ).to_dict()
    write(directory / f"{name}-record.json", record)
    return {
        "case_id": name,
        "task_id": task,
        "task_sha256": sha256(str(task).encode()).hexdigest(),
        "repetition": repetition,
        "split": split,
        "selection": "selected",
        "selection_reason": "planned",
        "synthetic": synthetic,
        "context": {
            "workload": "fixture-workload",
            "model": "fixture-model-config",
            "provider": "fixture-provider",
            "environment": "fixture-env",
            "scorer": "fixture-scorer",
            "policy": None,
            "cost_basis": cost_basis,
            "pricing": "fixture-pricing-v1" if cost_basis == "calculated" else None,
            "quality_gate": {"min_score": 0.9, "max_regression": 0},
            "intervention": {"type": "fixture-change", "configuration": {}},
        },
        "prediction": finding,
        "prediction_recorded_at": "2026-01-02T00:00:00Z",
        "outcome": {
            "record": f"{name}-record.json",
            "baseline_trace": f"{name}-baseline.json",
            "candidate_trace": f"{name}-candidate.json",
            "recorded_at": "2026-01-04T00:00:00Z",
            "status": status,
        },
    }


def manifest(directory, cases, **changes):
    payload = {
        "schema_version": "1.0",
        "name": "calibration unit fixtures",
        "as_of": "2026-01-05T00:00:00Z",
        "valid_until": "2026-02-01T00:00:00Z",
        "fit_method": "scale",
        "min_fit_tasks": 2,
        "selection_inventory_complete": True,
        "bootstrap": {"samples": 100, "seed": 7, "confidence": 0.95},
        "registrations": cases,
        **changes,
    }
    path = directory / "calibration.json"
    write(path, payload)
    return path


@pytest.fixture
def experiment(tmp_path):
    cases = [
        make_case(tmp_path, "fit-a", predicted=40, realized=20),
        make_case(tmp_path, "fit-b", predicted=80, realized=40),
        make_case(tmp_path, "held-a", predicted=20, realized=15, split="held_out"),
        make_case(tmp_path, "held-b", predicted=60, realized=30, split="held_out"),
    ]
    return tmp_path, cases


def test_historical_predictions_and_measurements_are_not_regenerated(experiment, monkeypatch):
    directory, cases = experiment
    path = manifest(directory, cases)

    def forbidden(*args, **kwargs):
        pytest.fail("historical evidence was recomputed")

    monkeypatch.setattr("agentloop.findings.build_diagnosis", forbidden)
    monkeypatch.setattr("agentloop.metrics.build_report", forbidden)
    report = summarize_calibration(path)
    assert report == summarize_calibration(path)
    assert len(report["cohorts"]) == 1
    metric = report["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["factor"] == 0.5
    held = metric["splits"]["held_out"]
    assert held["original"]["signed_error"]["task_summary"]["mean"] == 17.5
    assert held["original"]["realized_savings"]["task_summary"]["mean"] == 22.5
    assert held["scaled_error"]["task_summary"]["mean"] == -2.5
    assert not report["runtime_coefficients_changed"]
    assert (
        report["registrations"][0]["prediction_snapshot"]["estimate"]["estimator_version"]
        == "historic-v1"
    )


def test_failed_outcomes_are_not_dropped_from_raw_fit(tmp_path):
    cases = [
        make_case(tmp_path, "a", predicted=40, realized=20),
        make_case(
            tmp_path, "b", predicted=80, realized=40, status="failed", success=False, score=0
        ),
    ]
    report = summarize_calibration(manifest(tmp_path, cases))
    metric = report["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["factor"] == 0.5
    assert metric["fit"]["task_count"] == 2
    split = metric["splits"]["fit"]
    assert split["quality_gate_counts"] == {"accepted": 1, "rejected": 1, "indeterminate": 0}
    assert split["quality_acceptance_rate"] == 0.5
    assert split["original"]["signed_error"]["observation_summary"]["count"] == 2
    assert split["quality_preserving"]["signed_error"]["observation_summary"]["count"] == 1


def test_held_out_values_never_change_the_fit(experiment):
    directory, cases = experiment
    cases[-1] = make_case(directory, "held-b", predicted=600, realized=-50, split="held_out")
    metric = summarize_calibration(manifest(directory, cases))["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["factor"] == 0.5
    assert (
        metric["splits"]["held_out"]["original"]["realized_savings"]["task_summary"]["mean"]
        == -17.5
    )


def test_zero_predictions_and_negative_realized_savings_are_explicit(tmp_path):
    cases = [
        make_case(tmp_path, "a", predicted=0, realized=-10),
        make_case(tmp_path, "b", predicted=0, realized=10),
    ]
    report = summarize_calibration(manifest(tmp_path, cases))
    metric = report["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["status"] == "zero_predictions"
    assert all(
        row["metrics"]["latency"]["ratio_status"] == "zero_prediction"
        for row in report["registrations"]
    )
    assert metric["splits"]["fit"]["original"]["signed_error"]["observation_summary"]["count"] == 2
    assert (
        metric["splits"]["fit"]["original"]["realization_ratio"]["observation_summary"]["count"]
        == 0
    )
    assert all(
        row["metrics"]["cost"]["predicted_savings"] is None for row in report["registrations"]
    )


@pytest.mark.parametrize("selection", ["rejected", "unselected"])
def test_unselected_inventory_retained_with_zero_acceptance_denominator(tmp_path, selection):
    case = make_case(tmp_path, "a")
    case.update(selection=selection, selection_reason="not_chosen", outcome=None)
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert report["registration_count"] == 1
    split = report["cohorts"][0]["metrics"]["latency"]["splits"]["fit"]
    assert split["selected_count"] == 0 and split["quality_acceptance_rate"] is None
    assert split["selection_counts"] == {selection: 1}
    assert split["original"]["signed_error"]["observation_summary"]["missing_count"] == 1


def test_missing_outcome_file_remains_indeterminate(tmp_path):
    case = make_case(tmp_path, "a")
    case["outcome"]["record"] = "missing.json"
    report = summarize_calibration(manifest(tmp_path, [case]))
    row = report["registrations"][0]
    assert "missing_intervention" in row["issues"]
    assert row["quality_gate"] == "indeterminate"
    assert not row["metrics"]["latency"]["empirical_eligible"]


def test_synthetic_sources_are_excluded_even_if_registration_marker_is_false(tmp_path):
    case = make_case(tmp_path, "a", synthetic=True)
    case["synthetic"] = False
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert report["synthetic_excluded_count"] == 1
    split = report["cohorts"][0]["metrics"]["latency"]["splits"]["fit"]
    assert split["non_synthetic_registration_count"] == 0
    assert split["original"]["signed_error"]["observation_summary"]["count"] == 0


def test_original_snapshot_changes_cannot_be_laundered_through_new_estimator_versions(tmp_path):
    case = make_case(tmp_path, "a")
    case["prediction"]["savings"]["estimated_latency_savings_ms"] = 999
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert "original_prediction_mismatch" in report["registrations"][0]["issues"]
    assert not report["registrations"][0]["metrics"]["latency"]["empirical_eligible"]


def test_prediction_created_after_candidate_started_is_ineligible(tmp_path):
    case = make_case(tmp_path, "a")
    case["prediction_recorded_at"] = "2026-01-03T12:00:00Z"
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert "prediction_not_frozen_before_candidate" in report["registrations"][0]["issues"]


def test_changed_source_fingerprint_is_retained_but_not_calibrated(tmp_path):
    case = make_case(tmp_path, "a")
    path = tmp_path / case["outcome"]["candidate_trace"]
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["metadata"]["altered"] = True
    write(path, trace)
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert "candidate_source_mismatch" in report["registrations"][0]["issues"]
    assert report["registrations"][0]["intervention"] is not None


def test_task_split_leakage_is_rejected(tmp_path):
    first = make_case(tmp_path, "a", task="same")
    second = make_case(tmp_path, "b", task="same", split="held_out")
    with pytest.raises(CalibrationValidationError, match="cross splits"):
        summarize_calibration(manifest(tmp_path, [first, second]))


def test_duplicate_outcomes_are_not_counted_twice(experiment):
    directory, cases = experiment
    cases[1]["outcome"] = copy.deepcopy(cases[0]["outcome"])
    report = summarize_calibration(manifest(directory, cases))
    rows = {row["case_id"]: row for row in report["registrations"]}
    for name in ("fit-a", "fit-b"):
        assert "duplicate_outcome" in rows[name]["issues"]
        assert not rows[name]["metrics"]["latency"]["empirical_eligible"]


def test_repeated_seeds_do_not_become_independent_fit_tasks(tmp_path):
    cases = [
        make_case(
            tmp_path, f"a-{index}", task="a", repetition=str(index), predicted=40, realized=20
        )
        for index in range(5)
    ]
    report = summarize_calibration(manifest(tmp_path, cases))
    metric = report["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["status"] == "insufficient_tasks"
    assert (
        metric["splits"]["fit"]["original"]["signed_error"]["interval"]["status"]
        == "insufficient_tasks"
    )


@pytest.mark.parametrize("field", ["model", "provider", "environment", "scorer"])
def test_different_contexts_are_not_pooled(experiment, field):
    directory, cases = experiment
    cases[1]["context"][field] = "other-version"
    assert len(summarize_calibration(manifest(directory, cases))["cohorts"]) == 2


def test_estimator_versions_are_not_pooled(tmp_path):
    cases = [make_case(tmp_path, "a"), make_case(tmp_path, "b", version="historic-v2")]
    assert len(summarize_calibration(manifest(tmp_path, cases))["cohorts"]) == 2


def test_unknown_quality_does_not_become_passed_from_performance_gates(tmp_path):
    case = make_case(tmp_path, "a", score=None)
    report = summarize_calibration(manifest(tmp_path, [case]))
    row = report["registrations"][0]
    assert row["intervention"]["gates_passed"] is True
    assert row["quality_gate"] == "indeterminate"
    assert row["metrics"]["latency"]["empirical_eligible"]


def test_provider_billing_can_be_known_without_token_counts(tmp_path):
    case = make_case(
        tmp_path, "a", metric="cost", predicted=0.5, realized=0.25, token_provenance="unavailable"
    )
    report = summarize_calibration(manifest(tmp_path, [case]))
    metric = report["registrations"][0]["metrics"]["cost"]
    assert metric["empirical_eligible"]
    assert metric["signed_error"] == 0.25


def test_calculated_costs_require_exact_tokens_and_pricing_reference(tmp_path):
    case = make_case(
        tmp_path,
        "a",
        metric="cost",
        predicted=0.5,
        realized=0.25,
        token_provenance="unavailable",
        cost_basis="calculated",
    )
    report = summarize_calibration(manifest(tmp_path, [case]))
    metric = report["registrations"][0]["metrics"]["cost"]
    assert not metric["empirical_eligible"]
    assert "outcome_cost_provenance_incomplete" in metric["reasons"]
    case = make_case(tmp_path, "b", metric="cost", cost_basis="calculated")
    case["context"]["pricing"] = None
    report = summarize_calibration(manifest(tmp_path, [case]))
    assert "pricing_reference_missing" in report["registrations"][0]["metrics"]["cost"]["reasons"]


def test_empty_and_expired_artifacts_are_explicit(tmp_path):
    report = summarize_calibration(manifest(tmp_path, [], valid_until="2026-01-04T00:00:00Z"))
    assert report["registration_count"] == 0 and report["cohorts"] == []
    assert report["validity"]["expired"]
    assert "expired" in calibration_to_markdown(report)


@pytest.mark.parametrize(
    "target",
    ["calibration.json", "fit-a-record.json", "fit-a-baseline.json", "fit-a-candidate.json"],
)
def test_cli_preserves_all_source_evidence(experiment, target):
    directory, cases = experiment
    path = manifest(directory, cases)
    source = directory / target
    original = source.read_bytes()
    result = CliRunner().invoke(
        app,
        [
            "study",
            "calibrate",
            str(path),
            "--out",
            str(directory / "out.md"),
            "--json-out",
            str(source),
        ],
    )
    assert result.exit_code == 2
    assert source.read_bytes() == original
    assert not (directory / "out.md").exists()


def test_cli_writes_reports_without_changing_inputs(experiment):
    directory, cases = experiment
    path = manifest(directory, cases)
    original = path.read_bytes()
    result = CliRunner().invoke(
        app,
        [
            "study",
            "calibrate",
            str(path),
            "--out",
            str(directory / "report.md"),
            "--json-out",
            str(directory / "report.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(
        (directory / "report.json").read_text(encoding="utf-8")
    ) == summarize_calibration(path)
    assert path.read_bytes() == original
    assert "held_out" in (directory / "report.md").read_text(encoding="utf-8")


def test_public_example_is_reproducible_and_excludes_fixtures(tmp_path):
    example = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "intervention_calibration.py")
    )
    report = example["run"](tmp_path)
    serialized = (tmp_path / "report.json").read_bytes()
    assert example["run"](tmp_path) == report
    assert (tmp_path / "report.json").read_bytes() == serialized
    assert report["synthetic_excluded_count"] == report["registration_count"] == 6
    assert all(
        not value["empirical_eligible"]
        for row in report["registrations"]
        for value in row["metrics"].values()
    )


def test_claimed_provider_basis_cannot_relabel_calculated_costs(tmp_path):
    case = make_case(tmp_path, "a", metric="cost", cost_basis="calculated")
    case["context"]["cost_basis"] = "provider_reported"
    metric = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]["metrics"][
        "cost"
    ]
    assert "source_cost_basis_unverified" in metric["reasons"]
    assert not metric["empirical_eligible"]


def test_declared_policy_must_appear_in_enforced_source_evidence(tmp_path):
    case = make_case(tmp_path, "a")
    case["context"]["policy"] = {"id": "guard", "version": "1", "config_hash": "a" * 64}
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert "policy_not_observed_enforced" in row["issues"]
    assert not row["metrics"]["latency"]["empirical_eligible"]


def shared_baseline_cases(directory, *, same_prediction):
    cases = [make_case(directory, name, task="same-task") for name in ("a", "b")]
    if same_prediction:
        cases[1]["prediction"] = copy.deepcopy(cases[0]["prediction"])
    baseline = AgentTrace.from_json(directory / "a-baseline.json")
    candidate = AgentTrace.from_json(directory / "b-candidate.json")
    prediction = cases[1]["prediction"]
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[prediction["finding_id"]],
        intervention_type="fixture-change",
        diagnosis={"run_id": baseline.run_id, "findings": [prediction]},
        metadata={"synthetic": False},
    ).to_dict()
    write(directory / "b-record.json", record)
    cases[1]["outcome"]["baseline_trace"] = "a-baseline.json"
    return cases


def test_duplicate_pairing_keys_are_all_excluded(tmp_path):
    cases = shared_baseline_cases(tmp_path, same_prediction=True)
    report = summarize_calibration(manifest(tmp_path, cases))
    assert all("duplicate_pairing_key" in row["issues"] for row in report["registrations"])
    assert all(
        not row["metrics"]["latency"]["empirical_eligible"] for row in report["registrations"]
    )


def test_distinct_findings_on_one_task_are_not_duplicate_predictions(tmp_path):
    cases = shared_baseline_cases(tmp_path, same_prediction=False)
    report = summarize_calibration(manifest(tmp_path, cases))
    assert all(row["metrics"]["latency"]["empirical_eligible"] for row in report["registrations"])
    summary = report["cohorts"][0]["metrics"]["latency"]["splits"]["fit"]["original"][
        "signed_error"
    ]
    assert summary["observation_summary"]["count"] == 2
    assert summary["task_summary"]["count"] == 1


def test_task_means_give_each_task_one_fit_weight(tmp_path):
    cases = [
        make_case(
            tmp_path, f"a-{index}", task="a", repetition=str(index), predicted=20, realized=20
        )
        for index in range(5)
    ]
    cases.append(make_case(tmp_path, "b", predicted=20, realized=0))
    metric = summarize_calibration(manifest(tmp_path, cases))["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["factor"] == 0.5
    assert metric["fit"]["task_count"] == 2


def test_fit_can_be_disabled_without_losing_error_summaries(experiment):
    directory, cases = experiment
    report = summarize_calibration(manifest(directory, cases, fit_method="none"))
    metric = report["cohorts"][0]["metrics"]["latency"]
    assert metric["fit"]["status"] == "not_requested"
    assert metric["splits"]["held_out"]["original"]["signed_error"]["task_summary"]["mean"] == 17.5


def test_source_pairing_preserves_scalar_types(tmp_path):
    case = make_case(tmp_path, "a", task=1)
    case["task_id"] = "1"
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert "baseline_pairing_unverified" in row["issues"]


@pytest.mark.parametrize(
    "change",
    [
        {"fit_method": "automatic"},
        {"min_fit_tasks": 1},
        {"selection_inventory_complete": "yes"},
        {"as_of": "2026-01-05"},
        {"schema_version": "2.0"},
    ],
)
def test_malformed_manifest_fails_explicitly(tmp_path, change):
    with pytest.raises(CalibrationValidationError):
        summarize_calibration(manifest(tmp_path, [], **change))


@pytest.mark.parametrize("capture_gap", [False, True])
def test_native_enforced_policy_provenance_and_capture_gaps(tmp_path, monkeypatch, capture_gap):
    from agentloop.harness import Decision, Harness, HarnessConfig, Policy
    from agentloop.harness_evidence import METADATA_KEY
    from agentloop.tracer import bind_trace_context

    case = make_case(tmp_path, "a")
    baseline = AgentTrace.from_json(tmp_path / "a-baseline.json")
    candidate = AgentTrace.from_json(tmp_path / "a-candidate.json")
    policy = Policy("guard", "1", lambda context: Decision())
    monkeypatch.setattr("agentloop.harness.utc_now_iso", lambda: "2026-01-03T00:00:00Z")
    with bind_trace_context(candidate):
        run = Harness(HarnessConfig("enforce", (policy,))).start_run()
        run.wrap(lambda: None, boundary="model")()
    if capture_gap:
        candidate.metadata[METADATA_KEY]["capture_errors"].append(
            {"call_id": "missing", "phase": "after", "category": "trace_evidence_error"}
        )
    candidate.export_json(tmp_path / "a-candidate.json")
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[case["prediction"]["finding_id"]],
        intervention_type="fixture-change",
        diagnosis={"run_id": baseline.run_id, "findings": [case["prediction"]]},
        metadata={"synthetic": False},
    ).to_dict()
    write(tmp_path / "a-record.json", record)
    case["context"]["policy"] = {
        "id": policy.policy_id,
        "version": policy.version,
        "config_hash": policy.config_hash,
    }
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert row["metrics"]["latency"]["empirical_eligible"] is not capture_gap
    assert ("policy_not_observed_enforced" in row["issues"]) is capture_gap


def test_quality_threshold_uses_declared_decimal_values(tmp_path):
    case = make_case(tmp_path, "a", score=0.95)
    case["context"]["quality_gate"]["max_regression"] = 0.05
    assert (
        summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]["quality_gate"]
        == "accepted"
    )


def test_legacy_missing_estimator_provenance_is_not_regenerated(tmp_path):
    case = make_case(tmp_path, "a")
    case["prediction"].pop("estimate")
    record_path = tmp_path / "a-record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["predicted"]["findings"][0].pop("estimate")
    write(record_path, record)
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert "missing_estimator_provenance" in row["issues"]
    assert row["metrics"]["latency"]["predicted_savings"] is None


def test_invalid_outcome_artifacts_are_retained_as_unknown(tmp_path):
    case = make_case(tmp_path, "a")
    (tmp_path / "a-record.json").write_text("not a ledger", encoding="utf-8")
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert "invalid_intervention" in row["issues"]
    assert row["metrics"]["latency"]["realized_savings"] is None


def test_applied_intervention_must_match_frozen_planned_configuration(tmp_path):
    case = make_case(tmp_path, "a")
    case["context"]["intervention"]["configuration"] = {"different": True}
    row = summarize_calibration(manifest(tmp_path, [case]))["registrations"][0]
    assert "intervention_configuration_mismatch" in row["issues"]
    assert not row["metrics"]["latency"]["empirical_eligible"]


def test_missing_outcomes_stay_in_the_planned_configuration_cohort(experiment):
    directory, cases = experiment
    cases[1]["outcome"] = None
    report = summarize_calibration(manifest(directory, cases))
    assert len(report["cohorts"]) == 1
    split = report["cohorts"][0]["metrics"]["latency"]["splits"]["fit"]
    assert split["selected_count"] == 2
    assert split["quality_acceptance_rate"] == 0.5
    assert split["original"]["signed_error"]["observation_summary"]["missing_count"] == 1
