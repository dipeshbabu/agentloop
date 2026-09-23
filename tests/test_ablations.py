from __future__ import annotations

import copy
import json
import runpy
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.ablation_protocol import AblationProtocol, AblationValidationError
from agentloop.ablations import ablation_to_markdown, build_ablation_report, summarize_ablation
from agentloop.cli import app


def specification():
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
        "shadow": {"mode": "shadow", "policies": {"budget": "1:fixed-config"}},
        "enforce": {"mode": "enforce", "policies": {"budget": "1:fixed-config"}},
    }
    tasks = {
        name: {
            "split": "pilot" if name == "pilot" else "held_out",
            "input_sha256": sha256(name.encode()).hexdigest(),
        }
        for name in ("pilot", "a", "b")
    }
    schedule = []
    for task in tasks:
        for repetition in ("0", "1"):
            shift = len(schedule) % len(conditions)
            order = list(conditions)
            schedule.append(
                {
                    "task_id": task,
                    "repetition": repetition,
                    "cache_condition": "cold",
                    "order": order[shift:] + order[:shift],
                }
            )
    return {
        "schema_version": "1.0",
        "name": "synthetic ablation",
        "workload_id": "fixture",
        "permission_ref": "synthetic fixture",
        "frozen_at": "2026-01-01T00:00:00Z",
        "synthetic": True,
        "versions": {
            key: "fixture-v1"
            for key in (
                "source",
                "model",
                "provider",
                "configuration",
                "scorer",
                "runner",
                "environment",
                "tools",
                "reset",
            )
        },
        "tasks": tasks,
        "conditions": conditions,
        "schedule": schedule,
        "quality_gate": {"min_score": 0.9, "max_regression": 0.05},
        "bootstrap": {"samples": 200, "seed": 17, "confidence": 0.95},
    }


def observations(protocol):
    spec = protocol.to_dict()["specification"]
    rows = []
    for slot in spec["schedule"]:
        for position, condition in enumerate(slot["order"]):
            rows.append(
                {
                    "observation_id": f"{slot['task_id']}-{slot['repetition']}-{condition}",
                    "protocol_hash": protocol.protocol_hash,
                    "task_id": slot["task_id"],
                    "repetition": slot["repetition"],
                    "cache_condition": slot["cache_condition"],
                    "condition": condition,
                    "position": position,
                    "started_at": "2026-01-02T00:00:00Z",
                    "versions": dict(spec["versions"]),
                    "reset_confirmed": True,
                    "provider_seed": 7,
                    "status": "completed",
                    "stop_reason": None,
                    "success": True,
                    "quality_score": 1,
                    "metrics": {
                        "latency_ms": {"off": 100, "trace": 110, "shadow": 120, "enforce": 90}[
                            condition
                        ],
                        "tokens": 100,
                        "cost_usd": 0.1,
                        "model_calls": 1,
                        "tool_calls": 2,
                        "retries": 0,
                        "policy_eval_ms": 0 if condition in {"off", "trace"} else 1,
                    },
                    "token_status": "exact",
                    "cost_status": "complete",
                    "cost_basis": "provider_reported",
                    "trace_run_id": None,
                }
            )
    return rows


@pytest.fixture
def experiment():
    protocol = AblationProtocol.freeze(specification())
    return protocol, observations(protocol)


def comparison(report, *, kind="enforcement_effect", split="held_out"):
    return next(
        item for item in report["comparisons"] if item["kind"] == kind and item["split"] == split
    )


def candidate(rows):
    return next(row for row in rows if row["observation_id"] == "a-0-enforce")


def test_freeze_owns_configuration_and_detects_tampering():
    spec = specification()
    protocol = AblationProtocol.freeze(spec)
    spec["versions"]["model"] = "changed"
    assert protocol.to_dict()["specification"]["versions"]["model"] == "fixture-v1"
    changed = protocol.to_dict()
    changed["specification"]["quality_gate"]["min_score"] = 0
    with pytest.raises(AblationValidationError, match="hash"):
        AblationProtocol.from_dict(changed)


def test_paired_reports_are_reproducible_separate_splits_and_overheads(experiment):
    protocol, rows = experiment
    report = build_ablation_report(protocol, rows)
    assert report == build_ablation_report(protocol, list(reversed(rows)))
    assert report["planned_observation_count"] == report["observed_count"] == 24
    assert len(report["comparisons"]) == 8
    for kind, expected in (
        ("trace_overhead", 10),
        ("policy_shadow_overhead", 10),
        ("enforcement_effect", -30),
        ("total_policy_effect", -20),
    ):
        result = comparison(report, kind=kind)
        assert result["planned_pair_count"] == 4
        metric = result["descriptive_deltas"]["latency_ms"]
        assert metric["task_summary"]["mean"] == expected
        assert metric["interval"]["task_count"] == 2
        assert metric["interval"]["lower"] == metric["interval"]["upper"] == expected
        assert result["quality_acceptance_rate"] == 1
    pilot = comparison(report, split="pilot")["descriptive_deltas"]["latency_ms"]
    assert pilot["interval"]["status"] == "insufficient_tasks"
    assert pilot["pair_summary"]["count"] == 2
    markdown = ablation_to_markdown(report)
    assert "synthetic fixture" in markdown and "latency_ms" in markdown
    assert "not causal identification" in markdown


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out", "stopped", "incomplete"])
def test_early_stops_and_failures_cannot_win_by_doing_less(experiment, status):
    protocol, rows = experiment
    row = candidate(rows)
    row.update(status=status, stop_reason="budget_exhausted", success=False, quality_score=0)
    row["metrics"]["latency_ms"] = 1
    report = build_ablation_report(protocol, rows)
    result = comparison(report)
    assert result["quality_gate_counts"] == {
        "accepted": 3,
        "rejected": 1,
        "indeterminate": 0,
        "unmatched": 0,
    }
    assert result["quality_acceptance_rate"] == 0.75
    metric = result["quality_preserving_deltas"]["latency_ms"]
    assert metric["pair_summary"]["mean"] == -30
    assert metric["pair_summary"]["missing_count"] == 1
    assert metric["planned_pair_count"] == 4
    assert report["stop_reason_counts"] == {"budget_exhausted": 1}
    assert len(report["observations"]) == 24


@pytest.mark.parametrize("field", ["success", "quality_score"])
def test_missing_task_quality_never_falls_back_to_no_error_spans(experiment, field):
    protocol, rows = experiment
    candidate(rows)[field] = None
    result = comparison(build_ablation_report(protocol, rows))
    assert result["quality_gate_counts"]["indeterminate"] == 1
    assert result["quality_preserving_deltas"]["latency_ms"]["pair_summary"]["missing_count"] == 1


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_duplicate_pairs_keep_the_planned_denominator(experiment, duplicate):
    protocol, rows = experiment
    row = candidate(rows)
    if duplicate:
        rows.append({**copy.deepcopy(row), "observation_id": "second-attempt"})
    else:
        rows.remove(row)
    report = build_ablation_report(protocol, rows)
    result = comparison(report)
    assert result["planned_pair_count"] == 4
    assert result["quality_gate_counts"]["unmatched"] == 1
    assert result["quality_acceptance_rate"] == 0.75
    unmatched = next(case for case in result["cases"] if case["quality_gate"] == "unmatched")
    assert unmatched["reason"] == ("duplicate_pairing_key" if duplicate else "missing_counterpart")
    assert report["observed_count"] == (25 if duplicate else 23)


@pytest.mark.parametrize(
    "change",
    [
        {"token_status": "estimated"},
        {"cost_status": "partial"},
        {"cost_status": "unknown"},
        {"cost_basis": "calculated"},
        {"cost_basis": "mixed"},
    ],
)
def test_unknown_incomplete_or_mixed_measurements_are_not_zero(experiment, change):
    protocol, rows = experiment
    row = candidate(rows)
    row.update(change)
    result = comparison(build_ablation_report(protocol, rows))
    metric = "tokens" if "token_status" in change else "provider_cost_usd"
    assert result["descriptive_deltas"][metric]["pair_summary"]["missing_count"] == 1
    assert candidate(rows)["metrics"]["cost_usd"] == 0.1


def test_provider_cost_does_not_require_calculated_token_basis(experiment):
    protocol, rows = experiment
    candidate(rows)["token_status"] = "unavailable"
    result = comparison(build_ablation_report(protocol, rows))
    assert result["descriptive_deltas"]["provider_cost_usd"]["pair_summary"]["count"] == 4
    candidate(rows)["cost_basis"] = "calculated"
    report = build_ablation_report(protocol, rows)
    observed = next(
        row
        for row in report["observations"]
        if row["observation"]["observation_id"] == "a-0-enforce"
    )
    assert observed["evaluable_metrics"]["cost_usd"] is None


def test_provider_billing_and_calculated_costs_have_separate_summaries(experiment):
    protocol, rows = experiment
    for row in rows:
        if row["task_id"] == "b":
            row["cost_basis"] = "calculated"
            if row["condition"] == "enforce":
                row["metrics"]["cost_usd"] = 0.01
    result = comparison(build_ablation_report(protocol, rows))["descriptive_deltas"]
    assert "cost_usd" not in result
    assert result["provider_cost_usd"]["pair_summary"]["count"] == 2
    assert result["provider_cost_usd"]["pair_summary"]["mean"] == 0
    assert result["calculated_cost_usd"]["pair_summary"]["count"] == 2
    assert result["calculated_cost_usd"]["pair_summary"]["mean"] == pytest.approx(-0.09)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"position": 99}, "order_mismatch"),
        ({"reset_confirmed": False}, "reset_unconfirmed"),
        ({"started_at": "2025-01-01T00:00:00Z"}, "observed_before_freeze"),
        ({"protocol_hash": "other"}, "protocol_mismatch"),
        ({"versions": {}}, "version_mismatch"),
    ],
)
def test_protocol_deviations_remain_visible_and_unusable_for_comparison(experiment, change, reason):
    protocol, rows = experiment
    candidate(rows).update(change)
    report = build_ablation_report(protocol, rows)
    row = next(
        row
        for row in report["observations"]
        if row["observation"]["observation_id"] == "a-0-enforce"
    )
    assert reason in row["issues"]
    result = comparison(report)
    assert result["quality_gate_counts"]["indeterminate"] == 1
    assert result["descriptive_deltas"]["latency_ms"]["pair_summary"]["missing_count"] == 1


def test_unplanned_observations_are_retained_without_expanding_denominators(experiment):
    protocol, rows = experiment
    rows.append(
        {**copy.deepcopy(candidate(rows)), "observation_id": "extra", "task_id": "unplanned"}
    )
    report = build_ablation_report(protocol, rows)
    assert report["unplanned_observation_count"] == 1
    assert report["observed_count"] == 25
    assert comparison(report)["planned_pair_count"] == 4


def test_repetitions_are_not_independent_tasks_or_unequal_task_weights():
    spec = specification()
    spec["schedule"] = [slot for slot in spec["schedule"] if slot["repetition"] == "0"]
    template = next(slot for slot in spec["schedule"] if slot["task_id"] == "a")
    spec["schedule"].extend({**template, "repetition": str(index)} for index in range(1, 6))
    protocol = AblationProtocol.freeze(spec)
    rows = observations(protocol)
    for row in rows:
        if row["condition"] == "enforce":
            row["metrics"]["latency_ms"] = 100 if row["task_id"] == "a" else 20
    result = comparison(build_ablation_report(protocol, rows))["descriptive_deltas"]["latency_ms"]
    assert result["pair_summary"]["count"] == 7
    assert result["task_summary"]["mean"] == -60
    assert result["pair_summary"]["mean"] != -60
    assert result["interval"]["task_count"] == 2


def test_all_missing_runs_keep_full_denominators(experiment):
    protocol, _ = experiment
    report = build_ablation_report(protocol, [])
    assert report["observed_count"] == 0 and report["planned_observation_count"] == 24
    result = comparison(report)
    assert result["quality_gate_counts"]["unmatched"] == 4
    assert result["quality_acceptance_rate"] == 0
    assert result["descriptive_deltas"]["tokens"]["pair_summary"]["mean"] is None


def test_reader_roundtrip_without_traces_does_not_invent_them(tmp_path, experiment):
    protocol, rows = experiment
    path = tmp_path / "ablation.json"
    bundle = {
        "schema_version": "1.0",
        "protocol": protocol.to_dict(),
        "observations": rows,
        "study_manifest": None,
        "interventions": [],
    }
    path.write_text(json.dumps(bundle), encoding="utf-8")
    report = summarize_ablation(path)
    assert report == summarize_ablation(path)
    assert report["trace_study"] is None and report["interventions"] == []
    candidate(rows)["trace_run_id"] = "not-in-a-study"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(AblationValidationError, match="require an existing study"):
        summarize_ablation(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec["tasks"]["a"].update(input_sha256=spec["tasks"]["pilot"]["input_sha256"]),
        lambda spec: spec["tasks"]["pilot"].update(split="held_out"),
        lambda spec: spec["conditions"]["enforce"]["policies"].update(budget="different"),
        lambda spec: spec["schedule"][0].update(order=["off"] * 4),
        lambda spec: spec["schedule"].append(spec["schedule"][0]),
        lambda spec: spec["bootstrap"].update(samples=0),
    ],
)
def test_invalid_frozen_design_is_rejected(mutate):
    spec = specification()
    mutate(spec)
    with pytest.raises(AblationValidationError):
        AblationProtocol.freeze(spec)


@pytest.mark.parametrize(
    "key,value",
    [
        ("quality_score", True),
        ("success", 1),
        ("position", True),
        ("started_at", "2026-01-01"),
        ("cost_basis", "guess"),
    ],
)
def test_malformed_observations_fail_explicitly(experiment, key, value):
    protocol, rows = experiment
    candidate(rows)[key] = value
    with pytest.raises(AblationValidationError):
        build_ablation_report(protocol, rows)


@pytest.fixture
def example_bundle(tmp_path):
    example = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "harness_ablation.py")
    )
    report = example["run"](tmp_path)
    return tmp_path, report, example


def test_example_preserves_existing_study_and_ledger_and_is_reproducible(example_bundle):
    directory, report, example = example_bundle
    initial = (directory / "report.json").read_bytes()
    assert example["run"](directory) == report
    assert (directory / "report.json").read_bytes() == initial
    assert report["trace_study"]["conditions"]["trace"]["run_count"] == 6
    assert report["interventions"][0]["predicted"]["findings"]
    assert report["unlinked_trace_run_ids"] == []
    assert comparison(report)["quality_gate_counts"] == {
        "accepted": 1,
        "rejected": 1,
        "indeterminate": 1,
        "unmatched": 1,
    }
    assert "3 / 1 / 4" in (directory / "report.md").read_text(encoding="utf-8")


def test_linked_ledger_rejects_changed_trace_fingerprints(example_bundle):
    directory, _, _ = example_bundle
    source = directory / "trace" / "pilot-0-trace.json"
    trace = json.loads(source.read_text(encoding="utf-8"))
    trace["metadata"]["changed"] = True
    source.write_text(json.dumps(trace), encoding="utf-8")
    with pytest.raises(AblationValidationError, match="fingerprint"):
        summarize_ablation(directory / "bundle.json")


def test_trace_link_must_match_condition_and_pairing(example_bundle):
    directory, _, _ = example_bundle
    path = directory / "bundle.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    next(row for row in bundle["observations"] if row["condition"] == "enforce")["trace_run_id"] = (
        "pilot-0-trace"
    )
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(AblationValidationError, match="does not match"):
        summarize_ablation(path)


def test_synthetic_trace_marker_cannot_be_hidden_by_protocol(example_bundle):
    directory, _, _ = example_bundle
    path = directory / "bundle.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    spec = bundle["protocol"]["specification"]
    spec["synthetic"] = False
    protocol = AblationProtocol.freeze(spec)
    bundle["protocol"] = protocol.to_dict()
    for row in bundle["observations"]:
        row["protocol_hash"] = protocol.protocol_hash
    path.write_text(json.dumps(bundle), encoding="utf-8")
    report = summarize_ablation(path)
    assert report["includes_synthetic_data"]
    assert "Evidence: synthetic fixture" in ablation_to_markdown(report)


def test_cli_writes_reports_and_rejects_invalid_inputs(example_bundle):
    directory, report, _ = example_bundle
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "study",
            "ablation",
            str(directory / "bundle.json"),
            "--out",
            str(directory / "cli.md"),
            "--json-out",
            str(directory / "cli.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads((directory / "cli.json").read_text(encoding="utf-8")) == report
    bad = runner.invoke(app, ["study", "ablation", str(directory / "missing.json")])
    assert bad.exit_code == 2
    assert "cannot read ablation bundle" in bad.output


def test_new_policy_combinations_require_single_policy_ablations():
    spec = specification()
    for condition in ("shadow", "enforce"):
        spec["conditions"][condition]["policies"]["loops"] = "1:loop-config"
    with pytest.raises(AblationValidationError, match="single-policy"):
        AblationProtocol.freeze(spec)


def test_duplicate_observation_ids_and_nonfinite_metrics_fail(experiment):
    protocol, rows = experiment
    with pytest.raises(AblationValidationError, match="unique"):
        build_ablation_report(protocol, [*rows, rows[0]])
    candidate(rows)["metrics"]["latency_ms"] = float("nan")
    with pytest.raises(AblationValidationError, match="finite"):
        build_ablation_report(protocol, rows)


def test_quality_regression_accepts_the_declared_decimal_boundary():
    spec = specification()
    spec["quality_gate"] = {"min_score": 0.8, "max_regression": 0.05}
    protocol = AblationProtocol.freeze(spec)
    rows = observations(protocol)
    next(row for row in rows if row["observation_id"] == "a-0-shadow")["quality_score"] = 0.9
    candidate(rows)["quality_score"] = 0.85
    assert comparison(build_ablation_report(protocol, rows))["quality_gate_counts"]["accepted"] == 4


@pytest.mark.parametrize(
    "source", ["bundle.json", "study.json", "intervention.json", "trace/pilot-0-trace.json"]
)
def test_cli_does_not_overwrite_source_evidence(example_bundle, source):
    directory, _, _ = example_bundle
    target = directory / source
    original = target.read_bytes()
    result = CliRunner().invoke(
        app,
        [
            "study",
            "ablation",
            str(directory / "bundle.json"),
            "--out",
            str(directory / "out.md"),
            "--json-out",
            str(target),
        ],
    )
    assert result.exit_code == 2
    assert target.read_bytes() == original
    assert not (directory / "out.md").exists()


def test_cache_strata_keep_distinct_pairings_and_effects():
    spec = specification()
    spec["schedule"].extend({**slot, "cache_condition": "warm"} for slot in list(spec["schedule"]))
    protocol = AblationProtocol.freeze(spec)
    rows = observations(protocol)
    for row in rows:
        if row["cache_condition"] == "warm":
            row["observation_id"] += "-warm"
            if row["condition"] == "enforce":
                row["metrics"]["latency_ms"] += 500
    report = build_ablation_report(protocol, rows)
    effects = {
        item["cache_condition"]: item
        for item in report["comparisons"]
        if item["kind"] == "enforcement_effect" and item["split"] == "held_out"
    }
    assert effects["cold"]["descriptive_deltas"]["latency_ms"]["task_summary"]["mean"] == -30
    assert effects["warm"]["descriptive_deltas"]["latency_ms"]["task_summary"]["mean"] == 470
    assert all(item["planned_pair_count"] == 4 for item in effects.values())


def test_duplicate_ledger_links_are_rejected(example_bundle):
    directory, _, _ = example_bundle
    path = directory / "bundle.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    bundle["interventions"] *= 2
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(AblationValidationError, match="duplicate intervention"):
        summarize_ablation(path)


def test_stop_reason_rejects_unbounded_exception_text(experiment):
    protocol, rows = experiment
    candidate(rows)["stop_reason"] = "Exception: potentially private payload"
    with pytest.raises(AblationValidationError, match="bounded identifier"):
        build_ablation_report(protocol, rows)


def test_exported_integer_durations_keep_the_original_ledger_fingerprint(example_bundle):
    from agentloop.interventions import canonical_json

    directory, _, _ = example_bundle
    path = directory / "trace" / "pilot-0-trace.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    for event in trace["events"]:
        event["duration_ms"] = int(event["duration_ms"])
    path.write_text(json.dumps(trace), encoding="utf-8")
    ledger_path = directory / "intervention.json"
    record = json.loads(ledger_path.read_text(encoding="utf-8"))
    record["trace_fingerprints"]["baseline"] = sha256(canonical_json(trace).encode()).hexdigest()
    ledger_path.write_text(json.dumps(record), encoding="utf-8")
    assert summarize_ablation(directory / "bundle.json")["interventions"][0] == record
