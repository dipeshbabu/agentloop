"""Read-only job inventories, verifier criteria and conservative comparisons."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, app
from agentloop.html_report import analysis_to_html
from agentloop.integrations.harbor.manifest import write_harbor_study
from agentloop.integrations.harbor.trial_evidence import PAIRING_KEYS, read_trial
from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract, external_outcome
from agentloop.interoperability.validation import ImportLimits, ImportValidationError
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.studies import StudyValidationError, summarize_study
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures/external/harbor/mixed_job"
SCORING = ScoringContract.all_gte("synthetic-correctness-v1", {"correctness": 1.0})


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "job"
    shutil.copytree(FIXTURES, root)
    return root


def options(**kwargs):
    return HarborOptions(scoring=SCORING, synthetic_fixture=True, **kwargs)


def trial(root, *, quality=1.0, seconds=2, **kwargs):
    shutil.copytree(FIXTURES / "pass", root)
    result = read(root / "result.json")
    result["verifier_result"]["rewards"]["correctness"] = quality
    result["agent_execution"]["finished_at"] = f"2026-01-01T00:00:{seconds:02d}Z"
    result["agent_result"] = {
        "n_input_tokens": 12,
        "n_output_tokens": 6,
        "n_cache_tokens": 4,
        "cost_usd": 0.1,
    }
    result.update(kwargs)
    write(root / "result.json", result)
    write(root / "verifier/reward.json", {"correctness": quality})
    return root


def paired_options(job_id):
    return options(
        job_id=job_id, protocol_id="fixture-protocol", trial_context={"": {"repetition": 1}}
    )


def receipts(result):
    return {
        item.to_dict()["source_metadata"]["trial_directory"]: item.to_dict()
        for item in result.trial_receipts
    }


def test_mixed_job_retains_all_statuses_and_missing_trajectories(job):
    result = import_harbor(job, options=options())
    stats = result.inventory()
    assert stats["trials_discovered"] == 8
    assert stats["trials_imported"] == 4
    assert stats["missing_trajectories"] == 4
    assert stats["invalid_records"] == 0
    assert stats["quality_pass"] == 2 and stats["quality_fail"] == 2
    assert stats["quality_indeterminate"] == 4
    rows = receipts(result)
    for name, status in {
        "exception": "failed",
        "timeout": "timed_out",
        "cancelled": "cancelled",
    }.items():
        assert rows[name]["outcome"]["execution_status"] == status
        assert rows[name]["traces"] == []
        assert rows[name]["missing_trace_reason"] == "missing_artifact"
        assert rows[name]["outcome"]["quality_pass"] is None
    assert rows["missing_trajectory"]["outcome"]["quality_pass"] is True
    assert rows["missing_trajectory"]["traces"] == []
    assert rows["pass"]["identity_provenance"]["job_id"] == "calculated"
    assert rows["quality_fail"]["outcome"]["quality_pass"] is False


def test_positive_fractional_and_null_rewards_have_no_implicit_pass(job):
    result = import_harbor(job)
    rows = receipts(result)
    assert rows["uninterpreted"]["outcome"]["verifier_dimensions"] == {"correctness": 0.5}
    assert all(item["outcome"]["quality_pass"] is None for item in rows.values())
    assert result.inventory()["external_quality_uninterpreted"] == 4
    scores = ScoringContract.all_gte("two-required", {"correctness": 1, "coverage": 1})
    outcome = external_outcome(
        execution_status="completed",
        verifier_status="available",
        dimensions={"correctness": 0.5, "coverage": 1},
        scoring=scores,
    )
    assert outcome["quality_pass"] is False
    for dimensions in ({"correctness": 1}, {"correctness": None, "coverage": 1}):
        assert (
            external_outcome(
                execution_status="completed",
                verifier_status="available",
                dimensions=dimensions,
                scoring=scores,
            )["quality_pass"]
            is None
        )


@pytest.mark.parametrize(
    "thresholds", [{"correctness": True}, {"correctness": float("inf")}, {}, {"": 1}]
)
def test_invalid_scoring_fails_closed(thresholds):
    with pytest.raises(ImportValidationError):
        ScoringContract.all_gte("bad", thresholds)


def test_scoring_hash_mismatch_and_unknown_rule_are_rejected():
    value = SCORING.to_dict()
    value["config_sha256"] = "0" * 64
    with pytest.raises(ImportValidationError, match="hash"):
        ScoringContract.from_dict(value)
    value = SCORING.to_dict()
    value.pop("config_sha256")
    value["rule"] = "positive_reward"
    with pytest.raises(ImportValidationError):
        ScoringContract.from_dict(value)


def test_multistep_outcomes_and_requested_isolation_are_distinct(job):
    row = receipts(import_harbor(job, options=options()))["multistep"]
    steps = row["source_metadata"]["step_results"]
    assert [step["outcome"]["quality_pass"] for step in steps] == [True, False]
    assert [step["configured_verifier_mode"] for step in steps] == ["shared", "separate"]
    assert all(step["outcome"]["verifier_isolation"] == "unknown" for step in steps)
    assert row["outcome"]["quality_pass"] is None
    assert row["outcome"]["execution_status"] == "unknown"
    assert row["source_metadata"]["phase_timing"]["agent_execution"]["duration_ms"] is None


def test_trial_reward_cannot_hide_failed_step_verifier(job):
    path = job / "multistep/result.json"
    value = read(path)
    value["verifier_result"] = {"rewards": {"correctness": 1}}
    write(path, value)
    row = receipts(import_harbor(job, options=options()))["multistep"]
    assert row["outcome"]["quality_pass"] is None
    assert row["outcome"]["verifier_status"] == "invalid"


def test_trial_and_step_usage_are_not_added_and_missing_steps_stay_unknown(job):
    path = job / "multistep/result.json"
    value = read(path)
    value["step_results"][0]["agent_result"] = {
        "n_input_tokens": 100,
        "n_output_tokens": 3,
        "n_cache_tokens": 50,
        "cost_usd": 0.1,
    }
    value["step_results"][1]["agent_result"] = {
        "n_input_tokens": 20,
        "n_output_tokens": 2,
        "n_cache_tokens": 10,
    }
    write(path, value)
    row = receipts(import_harbor(job))["multistep"]
    assert row["source_metadata"]["usage"] == {
        "input_tokens": 120,
        "output_tokens": 5,
        "cached_input_tokens": 60,
        "cost_usd": None,
    }
    value["agent_result"] = {"n_input_tokens": 999}
    write(path, value)
    assert (
        receipts(import_harbor(job))["multistep"]["source_metadata"]["usage"]["input_tokens"] == 999
    )


@pytest.mark.parametrize(
    "artifact",
    [
        "pass/result.json",
        "pass/lock.json",
        "pass/verifier/reward.json",
        "pass/agent/trajectory.json",
    ],
)
def test_expected_hash_mismatch_invalidates_trial(job, artifact):
    result = import_harbor(job, options=options(expected_hashes={artifact: "0" * 64}))
    row = next(row for row in result.inventory()["trial_rows"] if row["trial_directory"] == "pass")
    assert row["import_status"] == "invalid"
    assert row.get("quality_pass") is None


def test_expected_missing_reward_is_not_an_available_stale_pass(job):
    (job / "pass/verifier/reward.json").unlink()
    result = import_harbor(
        job, options=options(expected_hashes={"pass/verifier/reward.json": "0" * 64})
    )
    assert receipts(result)["pass"]["outcome"]["quality_pass"] is None


@pytest.mark.parametrize(
    "corrupt",
    [
        "pass/result.json",
        "pass/lock.json",
        "pass/verifier/reward.json",
        "pass/agent/trajectory.json",
    ],
)
def test_independent_corrupt_artifacts_are_retained_without_losing_later_trials(job, corrupt):
    (job / corrupt).write_text('{"secret":private-token}', encoding="utf-8")
    result = import_harbor(job, options=options())
    assert result.inventory()["trials_discovered"] == 8
    assert result.inventory()["invalid_records"] == 1
    assert "private-token" not in json.dumps(result.inventory())
    assert receipts(result)["quality_fail"]["outcome"]["quality_pass"] is False


def test_failed_verifier_with_stale_positive_rewards_is_indeterminate(job):
    path = job / "pass/result.json"
    value = read(path)
    value["exception_info"] = {
        "exception_type": "VerifierError",
        "exception_message": "private-token",
        "exception_traceback": "secret traceback",
    }
    write(path, value)
    row = receipts(import_harbor(job, options=options()))["pass"]
    assert row["outcome"]["verifier_status"] == "failed"
    assert row["outcome"]["quality_pass"] is None
    assert row["outcome"]["verifier_dimensions"] == {"correctness": 1}
    assert "private-token" not in json.dumps(row)
    assert "secret traceback" not in json.dumps(row)


def test_contradictory_reward_files_fail_closed(job):
    write(job / "pass/verifier/reward.json", {"correctness": 0.5})
    row = receipts(import_harbor(job, options=options()))["pass"]
    assert row["outcome"]["quality_pass"] is None
    assert row["outcome"]["verifier_status"] == "invalid"


def test_strict_import_raises_instead_of_retaining_corrupt_metadata(job):
    (job / "pass/lock.json").write_text("invalid", encoding="utf-8")
    with pytest.raises(ImportValidationError):
        import_harbor(job, options=options(continue_on_error=False))


def test_late_metadata_failure_retains_inventory_without_orphan_trace(job, tmp_path):
    path = job / "pass/result.json"
    value = read(path)
    value["task_name"] = "x" * 70_000
    write(path, value)
    result = import_harbor(job, options=options())
    assert result.inventory()["invalid_records"] == 1
    result.write(tmp_path / "bounded-output")


def test_late_invalid_receipt_does_not_break_valid_trial_export(job, tmp_path):
    path = job / "pass/result.json"
    value = read(path)
    value["repetition"] = {"unsupported": "structured pair key"}
    write(path, value)
    result = import_harbor(job, options=options())
    assert result.inventory()["invalid_records"] == 1
    assert len(result.trial_traces) == 3
    result.write(tmp_path / "partial-output")


@pytest.mark.parametrize(
    "field,value",
    [
        ("step_results", {}),
        ("task_id", []),
        ("verifier_environment_mode", {}),
        ("trial_name", ["bad"]),
        ("task_checksum", 4),
    ],
)
def test_invalid_structural_fields_are_diagnostics_not_unhandled_exceptions(job, field, value):
    path = job / "pass/result.json"
    source = read(path)
    source[field] = value
    write(path, source)
    result = import_harbor(job, options=options())
    assert result.inventory()["invalid_records"] == 1


def test_limits_apply_to_the_whole_job(job):
    for limits in (
        replace(ImportLimits(), max_trials=7),
        replace(ImportLimits(), max_trajectories=4),
        replace(ImportLimits(), max_total_bytes=100),
    ):
        with pytest.raises(ImportValidationError, match="budget|bounds"):
            import_harbor(job, limits=limits)


def test_duplicate_trial_ids_in_one_job_are_structural_error(job):
    path = job / "quality_fail/result.json"
    value = read(path)
    value["id"] = read(job / "pass/result.json")["id"]
    write(path, value)
    with pytest.raises(ImportValidationError, match="duplicate trial"):
        import_harbor(job)


def test_repeated_import_and_export_are_stable_and_conflicting_output_rejected(job, tmp_path):
    first, second = import_harbor(job, options=options()), import_harbor(job, options=options())
    assert first.inventory() == second.inventory()
    assert [trace.to_dict() for trace in first.traces] == [
        trace.to_dict() for trace in second.traces
    ]
    out = tmp_path / "out"
    first.write(out)
    second.write(out)
    for receipt in (*first.trial_receipts, *first.trajectory_receipts):
        for reference in receipt.to_dict()["traces"]:
            assert (
                sha256((out / reference["trace_file"]).read_bytes()).hexdigest()
                == reference["trace_sha256"]
            )
    (out / "harbor-inventory.json").write_text("conflict", encoding="utf-8")
    with pytest.raises(ImportValidationError, match="conflicts"):
        first.write(out)


def test_unknown_phase_cost_usage_stay_unavailable_in_analysis_and_html(job):
    result = import_harbor(job, options=options())
    for trace in result.trial_traces:
        restored = AgentTrace.from_dict(trace.to_dict())
        assert read_trial(restored) == read_trial(trace)
        analysis = _analysis_payload(restored)
        report = analysis["report"]
        assert report["input_tokens"] is None and report["estimated_cost_usd"] is None
        assert report["model_call_count"] is None
        assert report["tool_call_count"] is None
        assert report["retry_count"] is None
        assert "unavailable" in analysis_to_html(analysis)
        if trace.name == "multistep":
            assert report["total_runtime_ms"] is None
            assert analysis["optimization"]["estimated_after"]["runtime_ms"] is None


def test_job_id_and_duplicate_trial_ids_across_jobs_do_not_collide(tmp_path):
    left = import_harbor(trial(tmp_path / "left"), options=paired_options("job-left"))
    right = import_harbor(trial(tmp_path / "right"), options=paired_options("job-right"))
    assert left.trial_traces[0].run_id != right.trial_traces[0].run_id
    assert left.traces[0].run_id != right.traces[0].run_id
    assert left.trial_receipts[0].receipt_id != right.trial_receipts[0].receipt_id


def test_faster_incorrect_candidate_fails_external_quality_gate(tmp_path):
    base = import_harbor(
        trial(tmp_path / "base", seconds=5), options=paired_options("baseline")
    ).trial_traces[0]
    candidate = import_harbor(
        trial(tmp_path / "candidate", seconds=1, quality=0.5), options=paired_options("candidate")
    ).trial_traces[0]
    report = build_replay_report(
        base, candidate, gates=ReplayGates(require_retry_non_increase=False)
    )
    assert report["deltas"]["latency_improvement_pct"] > 0
    assert report["gates"]["passed"] is False
    gate = next(
        gate for gate in report["gates"]["results"] if gate["name"] == "external_task_correctness"
    )
    assert gate["passed"] is False and not gate["indeterminate"]
    assert report["deltas"]["model_call_count_delta"] is None


@pytest.mark.parametrize("mismatch", PAIRING_KEYS)
def test_incompatible_or_missing_pairing_cannot_replay(tmp_path, mismatch):
    base = import_harbor(trial(tmp_path / "base"), options=paired_options("baseline")).trial_traces[
        0
    ]
    candidate_root = trial(tmp_path / "candidate")
    opts = paired_options("candidate")
    if mismatch == "task_digest":
        for name in ("result.json", "lock.json"):
            path = candidate_root / name
            value = read(path)
            if name == "result.json":
                value["task_checksum"] = "b" * 64
            else:
                value["task"]["digest"] = "sha256:" + "b" * 64
            write(path, value)
    elif mismatch == "scorer_config_hash":
        opts = replace(opts, scoring=ScoringContract.all_gte("different", {"correctness": 1}))
    elif mismatch == "environment_hash":
        path = candidate_root / "lock.json"
        value = read(path)
        value["environment"]["type"] = "modal"
        write(path, value)
    elif mismatch == "protocol_id":
        opts = replace(opts, protocol_id="other")
    else:
        opts = replace(opts, trial_context={})
    candidate = import_harbor(candidate_root, options=opts).trial_traces[0]
    with pytest.raises(ImportValidationError, match="missing or incompatible"):
        build_replay_report(base, candidate)


def test_tampering_with_trace_or_evidence_is_rejected(tmp_path):
    trace = import_harbor(trial(tmp_path / "trial"), options=paired_options("job")).trial_traces[0]
    trace.elapsed_ms = 0
    with pytest.raises(ImportValidationError, match="no longer match"):
        trace.report()


def test_native_study_keeps_quality_and_population_exclusions_explicit(job, tmp_path):
    context = {row.name: {"repetition": row.name} for row in job.iterdir() if row.is_dir()}
    baseline = import_harbor(
        job,
        options=options(
            job_id="base", condition="baseline", protocol_id="protocol", trial_context=context
        ),
    )
    candidate = import_harbor(
        job,
        options=options(
            job_id="candidate", condition="candidate", protocol_id="protocol", trial_context=context
        ),
    )
    path = write_harbor_study(
        {"baseline": baseline, "candidate": candidate}, tmp_path / "study", baseline="baseline"
    )
    report = summarize_study(path)
    assert report["conditions"]["candidate"]["run_count"] == 4
    assert report["conditions"]["candidate"]["metrics"]["success"]["mean"] == pytest.approx(1 / 3)
    population = read(path.parent / "cohort-inventory.json")["conditions"]["candidate"]
    assert population["trials_discovered"] == 8
    assert len(population["excluded_trial_rows"]) == 4
    manifest = read(path)
    manifest["pairing_keys"] = ["repetition"]
    write(path, manifest)
    with pytest.raises(StudyValidationError, match="pairing keys"):
        summarize_study(path)


def test_legacy_result_name_requires_explicit_opt_in(tmp_path):
    root = trial(tmp_path / "legacy")
    (root / "result.json").rename(root / "results.json")
    default = import_harbor(root, options=options())
    assert default.trial_receipts[0].to_dict()["outcome"]["execution_status"] == "missing"
    legacy = import_harbor(root, options=options(allow_legacy_results_name=True))
    assert legacy.trial_receipts[0].to_dict()["outcome"]["execution_status"] == "completed"


def test_missing_planned_trials_and_embedded_results_are_retained(job):
    write(
        job / "result.json",
        {
            "id": "job-id",
            "n_total_trials": 10,
            "trial_results": [
                {"trial_name": "undispatched", "id": "missing-id", "task_checksum": "a" * 64}
            ],
        },
    )
    result = import_harbor(job)
    assert result.inventory()["trials_discovered"] == 9
    assert result.inventory()["planned_unidentified_trials"] == 1
    assert receipts(result)["undispatched"]["traces"] == []
    assert all(
        item["identity_provenance"]["job_id"] == "external_reported"
        for item in receipts(result).values()
    )


def test_job_without_any_observed_trial_reports_planned_missing_population(tmp_path):
    write(tmp_path / "result.json", {"id": "job-id", "n_total_trials": 3})
    result = import_harbor(tmp_path)
    assert result.inventory()["trials_discovered"] == 0
    assert result.inventory()["planned_unidentified_trials"] == 3
    assert not result.traces and not result.trial_traces


def test_import_never_executes_task_or_verifier_scripts(job, tmp_path):
    marker = tmp_path / "EXECUTED"
    (job / "pass/verifier/tests.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    import_harbor(job)
    assert not marker.exists()


def test_cli_import_and_inspect_with_explicit_scoring(job, tmp_path):
    scorer = tmp_path / "scoring.json"
    write(scorer, SCORING.to_dict())
    runner = CliRunner()
    out = tmp_path / "output"
    imported = runner.invoke(
        app,
        ["harbor", "import", str(job), "--out", str(out), "--scoring", str(scorer), "--synthetic"],
    )
    assert imported.exit_code == 0, imported.output
    assert "8 trials retained" in imported.output
    assert read(out / "harbor-inventory.json")["quality_fail"] == 2
    inspected = runner.invoke(app, ["harbor", "inspect", str(job), "--scoring", str(scorer)])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["trials_discovered"] == 8
    invalid = runner.invoke(app, ["harbor", "import", str(job / "missing"), "--out", str(out)])
    assert invalid.exit_code != 0
