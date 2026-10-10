"""Full trial populations, exact task pairing and independent external quality."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.integrations.harbor.manifest import write_harbor_study
from agentloop.integrations.harbor.trial_evidence import TRIAL_KEY, attach_trial
from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract
from agentloop.interoperability.cohort_evidence import COHORT_KEY, read_cohort
from agentloop.interoperability.cohorts import (
    CohortProtocol,
    materialize_source_study,
    write_cohort_study,
)
from agentloop.interoperability.validation import ImportValidationError
from agentloop.interventions import canonical_json
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.studies import StudyValidationError, summarize_study
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures/external/harbor/mixed_job"
SCORER = ScoringContract.all_gte("owned-correctness-v1", {"correctness": 1})
PROTOCOL = CohortProtocol("owned-protocol-v1")


@pytest.mark.parametrize("writer", ["cohort", "native_manifest"])
@pytest.mark.parametrize("condition_index", [0, 1])
@pytest.mark.parametrize("dangling", [False, True])
def test_studies_reject_linked_condition_folders_before_any_export(
    tmp_path, writer, condition_index, dangling
):
    conditions = cohort(tmp_path / "sources")
    out, outside = tmp_path / "report", tmp_path / "outside"
    out.mkdir()
    outside.mkdir()
    target = outside / "missing" if dangling else outside
    try:
        (out / f"condition-{condition_index}").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit directory symlink creation")
    with pytest.raises(ImportValidationError) as caught:
        if writer == "cohort":
            write_cohort_study(conditions, out, baseline="baseline", protocol=PROTOCOL)
        else:
            write_harbor_study(conditions, out, baseline="baseline")
    assert caught.value.code == "unsafe_path"
    assert not list(outside.iterdir())
    assert not (out / f"condition-{1 - condition_index}").exists()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value) + "\n").encode("utf-8"))


def trial(
    root,
    name,
    *,
    task="blue",
    digest="a" * 64,
    repetition=1,
    seconds=4,
    quality=1,
    execution=None,
    trajectory=True,
    usage=True,
    model="fixture-model",
    agent="fixture",
    **changes,
):
    target = root / name
    shutil.copytree(FIXTURES / "pass", target)
    result = read(target / "result.json")
    result.update(
        id=name,
        trial_name=name,
        task_name=task,
        task_id={"path": f"tasks/{task}"},
        task_checksum=digest,
        repetition=repetition,
        protocol_id=PROTOCOL.protocol_id,
        **changes,
    )
    result["agent_execution"]["finished_at"] = f"2026-01-01T00:00:{seconds:02d}Z"
    result["started_at"], result["finished_at"] = (
        "2026-01-01T00:00:00Z",
        f"2026-01-01T00:00:{seconds + 1:02d}Z",
    )
    result["agent_result"] = (
        {"n_input_tokens": 12, "n_output_tokens": 6, "n_cache_tokens": 4, "cost_usd": 0.1}
        if usage
        else None
    )
    result["agent_info"]["name"] = agent
    result["agent_info"]["model_info"]["name"] = model
    result["verifier_result"] = {"rewards": {"correctness": quality}}
    if execution is not None:
        result["exception_info"] = {
            "exception_type": execution,
            "exception_message": "PRIVATE_SOURCE_ERROR",
            "exception_traceback": "PRIVATE_STACK",
        }
    lock = read(target / "lock.json")
    lock["task"]["digest"] = "sha256:" + digest
    write(target / "lock.json", lock)
    write(target / "result.json", result)
    write(target / "verifier/reward.json", {"correctness": quality})
    if not trajectory:
        (target / "agent/trajectory.json").unlink()
    return target


def imported(root, condition, scoring=SCORER, **kwargs):
    return import_harbor(
        root,
        options=HarborOptions(
            job_id=f"owned-{condition}",
            condition=condition,
            protocol_id=PROTOCOL.protocol_id,
            scoring=scoring,
            synthetic_fixture=True,
            **kwargs,
        ),
    )


def cohort(tmp_path, *, candidate_quality=1, candidate_seconds=2, candidate_changes=None):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "before")
    trial(
        right,
        "after",
        seconds=candidate_seconds,
        quality=candidate_quality,
        **(candidate_changes or {}),
    )
    return {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")}


def test_correct_candidate_wins_and_faster_incorrect_candidate_is_rejected(tmp_path):
    good = write_cohort_study(
        cohort(tmp_path / "good"), tmp_path / "good-out", baseline="baseline", protocol=PROTOCOL
    ).report()
    comparison = good["comparisons"]["candidate"]
    assert comparison["paired_count"] == 1
    assert comparison["pairs"][0]["state"] == "verified_quality_preserving_improvement"
    assert comparison["pairs"][0]["runtime_improvement_pct"] == 50
    assert comparison["quality_preserving_intervention_rate"]["numerator"] == 1
    bad = write_cohort_study(
        cohort(tmp_path / "bad", candidate_quality=0),
        tmp_path / "bad-out",
        baseline="baseline",
        protocol=PROTOCOL,
    ).report()
    pair = bad["comparisons"]["candidate"]["pairs"][0]
    assert pair["runtime_improvement_pct"] == 50
    assert pair["state"] == "rejected"
    assert bad["comparisons"]["candidate"]["quality_preserving_intervention_rate"]["numerator"] == 0


def test_missing_trajectory_and_unknown_usage_do_not_become_successful_native_runs(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "before")
    trial(right, "missing", trajectory=False)
    trial(right, "unknown-usage", repetition=2, usage=False)
    result = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    )
    population = result.report()["conditions"]["candidate"]
    assert population["observed_attempts"] == population["denominator"] == 2
    assert population["quality_pass"] == 2
    assert population["accepted"] == 0 and population["acceptance_indeterminate"] == 2
    native = summarize_study(result.manifest_path)
    assert native["conditions"]["candidate"]["run_count"] == 2
    assert native["conditions"]["candidate"]["metrics"]["success"]["count"] == 0
    paths = read(result.manifest_path)["conditions"]["candidate"]
    for path in paths:
        restored = AgentTrace.from_dict(read(result.manifest_path.parent / path))
        assert not restored.events and restored.metadata["execution_data_present"] is False
        assert read_cohort(restored)["accepted"] is None


def test_failures_timeout_cancellation_missing_and_unpaired_trials_remain_in_denominator(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "before")
    trial(right, "pass", seconds=2)
    for index, error in enumerate(("AgentError", "TimeoutError", "CancelledError"), start=2):
        trial(right, error, repetition=index, execution=error, trajectory=False)
    trial(right, "no-trajectory", repetition=5, trajectory=False)
    trial(right, "unpaired", repetition=6)
    write(right / "result.json", {"id": "candidate-job", "n_total_trials": 8})
    report = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    ).report()
    population = report["conditions"]["candidate"]
    assert population["denominator"] == 8 and population["observed_attempts"] == 6
    assert population["planned_unidentified_attempts"] == 2
    assert population["execution_status_counts"]["failed"] == 1
    assert population["execution_status_counts"]["timed_out"] == 1
    assert population["execution_status_counts"]["cancelled"] == 1
    comparison = report["comparisons"]["candidate"]
    assert comparison["unpaired_count"] == 5
    assert comparison["quality_preserving_intervention_rate"]["denominator"] == 8
    assert comparison["quality_preserving_intervention_rate"]["numerator"] == 1
    assert "PRIVATE_SOURCE_ERROR" not in json.dumps(report) and "PRIVATE_STACK" not in json.dumps(
        report
    )


@pytest.mark.parametrize(
    "mismatch",
    ["task_digest", "task_id", "verifier_config_hash", "environment_hash", "scorer_config_hash"],
)
def test_exact_identity_and_configuration_mismatches_are_not_paired(tmp_path, mismatch):
    conditions = cohort(tmp_path)
    root = tmp_path / "candidate/after"
    if mismatch == "task_digest":
        value = read(root / "result.json")
        value["task_checksum"] = "b" * 64
        write(root / "result.json", value)
        lock = read(root / "lock.json")
        lock["task"]["digest"] = "sha256:" + "b" * 64
        write(root / "lock.json", lock)
    elif mismatch == "task_id":
        value = read(root / "result.json")
        value["task_id"]["path"] = "tasks/another"
        write(root / "result.json", value)
    elif mismatch == "verifier_config_hash":
        value = read(root / "config.json")
        value["verifier"]["disable"] = True
        write(root / "config.json", value)
    elif mismatch == "environment_hash":
        value = read(root / "lock.json")
        value["environment"]["type"] = "other-backend"
        write(root / "lock.json", value)
    conditions["candidate"] = imported(
        tmp_path / "candidate",
        "candidate",
        scoring=ScoringContract.all_gte("different", {"correctness": 1})
        if mismatch == "scorer_config_hash"
        else SCORER,
    )
    comparison = write_cohort_study(
        conditions, tmp_path / "out", baseline="baseline", protocol=PROTOCOL
    ).report()["comparisons"]["candidate"]
    assert comparison["paired_count"] == 0
    assert comparison["unpaired_count"] == 2
    assert comparison["quality_preserving_intervention_rate"]["numerator"] == 0


def test_partial_positive_reward_without_a_contract_is_indeterminate_and_unpaired(tmp_path):
    conditions = cohort(tmp_path, candidate_quality=0.5)
    conditions = {name: imported(tmp_path / name, name, scoring=None) for name in conditions}
    report = write_cohort_study(
        conditions, tmp_path / "out", baseline="baseline", protocol=PROTOCOL
    ).report()
    candidate = report["conditions"]["candidate"]
    assert candidate["quality_indeterminate"] == candidate["denominator"] == 1
    assert candidate["trial_rows"][0]["outcome"]["verifier_dimensions"] == {"correctness": 0.5}
    assert report["comparisons"]["candidate"]["paired_count"] == 0


def test_cross_vendor_model_difference_invalidates_causal_interpretation(tmp_path):
    conditions = cohort(
        tmp_path, candidate_changes={"agent": "other-harness", "model": "other-model"}
    )
    report = write_cohort_study(
        conditions,
        tmp_path / "out",
        baseline="baseline",
        protocol=replace(PROTOCOL, comparison_kind="controlled_harness_ablation"),
    ).report()
    comparison = report["comparisons"]["candidate"]
    assert comparison["controlled_design_validated"] is False
    assert "agent_info_differs" in comparison["pairs"][0]["confounders"]
    assert comparison["pairs"][0]["causal_harness_effect"] == "not_established"


def test_repeated_seeds_on_one_task_do_not_create_independent_task_samples(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    for seed in range(1, 5):
        trial(left, f"before-{seed}", repetition=seed)
        trial(right, f"after-{seed}", repetition=seed, seconds=2)
    report = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    ).report()
    uncertainty = report["comparisons"]["candidate"]["runtime_task_uncertainty"]
    assert uncertainty["planned_observation_count"] == 4
    assert uncertainty["interval"]["task_count"] == 1
    assert uncertainty["interval"]["status"] == "insufficient_tasks"


def test_duplicate_attempt_keys_are_ambiguous_not_manufactured_replications(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "first", repetition=1)
    trial(left, "repeated", repetition=1)
    trial(right, "after", repetition=1, seconds=2)
    comparison = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    ).report()["comparisons"]["candidate"]
    assert comparison["paired_count"] == 0
    assert all(row["reason"] == "duplicate_pairing_key" for row in comparison["unmatched"])


def test_empty_observed_condition_retains_planned_gaps_without_invented_traces(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "before")
    write(right / "result.json", {"id": "empty-job", "n_total_trials": 2})
    result = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    )
    assert result.manifest_path is None
    assert result.report()["conditions"]["candidate"]["denominator"] == 2
    assert result.report()["conditions"]["candidate"]["observed_attempts"] == 0


def test_invalid_metadata_trial_survives_as_an_inventory_observation(tmp_path):
    left, right = tmp_path / "baseline", tmp_path / "candidate"
    trial(left, "before")
    trial(right, "invalid")
    (right / "invalid/result.json").write_text("invalid PRIVATE_PAYLOAD", encoding="utf-8")
    result = write_cohort_study(
        {"baseline": imported(left, "baseline"), "candidate": imported(right, "candidate")},
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
    )
    assert result.report()["conditions"]["candidate"]["observed_attempts"] == 1
    assert result.report()["conditions"]["candidate"]["accepted"] == 0
    assert "PRIVATE_PAYLOAD" not in json.dumps(result.report())


def test_declared_reject_missingness_policy_is_explicit(tmp_path):
    conditions = cohort(tmp_path, candidate_changes={"trajectory": False})
    report = write_cohort_study(
        conditions,
        tmp_path / "out",
        baseline="baseline",
        protocol=replace(PROTOCOL, missingness_policy="reject"),
    ).report()
    assert report["conditions"]["candidate"]["rejected"] == 1
    assert report["protocol"]["missingness_policy"] == "reject"


def test_native_cohort_observation_mutation_and_weak_pairing_are_rejected(tmp_path):
    result = write_cohort_study(
        cohort(tmp_path), tmp_path / "out", baseline="baseline", protocol=PROTOCOL
    )
    manifest = read(result.manifest_path)
    paths = manifest["conditions"]
    base = AgentTrace.from_dict(read(result.manifest_path.parent / paths["baseline"][0]))
    candidate = AgentTrace.from_dict(read(result.manifest_path.parent / paths["candidate"][0]))
    replay = build_replay_report(
        base, candidate, gates=ReplayGates(require_retry_non_increase=False)
    )
    assert replay["gates"]["passed"] is True
    candidate.metadata[COHORT_KEY]["trajectory_available"] = False
    with pytest.raises(ImportValidationError):
        candidate.report()
    manifest["pairing_keys"] = ["task_id", "repetition"]
    write(result.manifest_path, manifest)
    with pytest.raises(StudyValidationError):
        summarize_study(result.manifest_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("import_status", []),
        ("condition", 42),
        ("source_receipt_sha256", "not-a-digest"),
        ("verifier_config_hash", True),
        ("source_receipt_id", None),
    ],
)
def test_recaptured_cohort_still_requires_typed_source_identity(tmp_path, field, value):
    result = write_cohort_study(
        cohort(tmp_path), tmp_path / "out", baseline="baseline", protocol=PROTOCOL
    )
    manifest = read(result.manifest_path)
    trace = AgentTrace.from_dict(
        read(result.manifest_path.parent / manifest["conditions"]["candidate"][0])
    )
    original = trace.metadata.pop(TRIAL_KEY)
    record = trace.metadata[COHORT_KEY]
    record[field] = value
    if field in {"condition", "verifier_config_hash"}:
        trace.metadata[field] = value
    record["sha256"] = sha256(
        canonical_json({key: item for key, item in record.items() if key != "sha256"}).encode()
    ).hexdigest()
    attach_trial(
        trace,
        {
            key: original[key]
            for key in ("receipt_id", "source_result", "measurement", "outcome", "pairing")
        },
    )
    with pytest.raises(ImportValidationError) as error:
        read_cohort(trace)
    assert error.value.code == "invalid_cohort_evidence"


def test_task_specific_scoring_and_exact_thresholds_are_retained(tmp_path):
    root = tmp_path / "jobs"
    trial(root, "task-a", quality=0.5)
    trial(root, "task-b", digest="b" * 64, task="second", quality=0.5)
    result = imported(
        root,
        "baseline",
        scoring_by_task_digest={
            "sha256:" + "b" * 64: ScoringContract.all_gte("second-task", {"correctness": 0.5})
        },
    )
    rows = {
        receipt.to_dict()["source_metadata"]["trial_name"]: receipt.to_dict()
        for receipt in result.trial_receipts
    }
    assert rows["task-a"]["outcome"]["quality_pass"] is False
    assert rows["task-b"]["outcome"]["quality_pass"] is True
    assert rows["task-b"]["outcome"]["scoring_contract"]["scorer_id"] == "second-task"


def manifest_sources(tmp_path):
    conditions = cohort(tmp_path)
    del conditions

    def source_config(path):
        return {
            "system": "harbor",
            "job_path": path,
            "scoring": SCORER.to_dict(),
            "task_scoring": {},
            "trial_context": {},
            "expected_hashes": {},
            "expected_agent": "fixture",
            "expected_model": "fixture-model",
        }

    payload = {
        "schema_version": "1.0",
        "name": "owned source study",
        "baseline": "baseline",
        "protocol": {"protocol_id": PROTOCOL.protocol_id},
        "sources": {"baseline": source_config("baseline"), "candidate": source_config("candidate")},
    }
    path = tmp_path / "sources.json"
    write(path, payload)
    return path


def test_source_manifest_and_cli_run_completed_artifacts_only(tmp_path):
    path = manifest_sources(tmp_path)
    result = materialize_source_study(path, tmp_path / "out")
    assert result.report()["comparisons"]["candidate"]["paired_count"] == 1
    cli = CliRunner().invoke(app, ["harbor", "study", str(path), "--out", str(tmp_path / "cli")])
    assert cli.exit_code == 0, cli.output


@pytest.mark.parametrize("mutation", ["command", "remote", "mismatch", "future"])
def test_source_manifest_rejects_executable_remote_or_false_identity_data(tmp_path, mutation):
    path = manifest_sources(tmp_path)
    value = read(path)
    if mutation == "command":
        value["command"] = "never execute"
    elif mutation == "remote":
        value["sources"]["candidate"]["job_path"] = "https://example.invalid/jobs"
    elif mutation == "mismatch":
        value["sources"]["candidate"]["expected_model"] = "false-model"
    else:
        value["schema_version"] = "future"
    write(path, value)
    with pytest.raises((ImportValidationError, OSError)):
        materialize_source_study(path, tmp_path / "out")


def test_captured_protocol_identity_is_not_silently_relabelled(tmp_path):
    with pytest.raises(ImportValidationError, match="protocol"):
        write_cohort_study(
            cohort(tmp_path),
            tmp_path / "out",
            baseline="baseline",
            protocol=CohortProtocol("different"),
        )


def test_original_finding_prediction_and_measured_outcome_snapshots_are_preserved(tmp_path):
    from agentloop.entrypoint import _quickstart_trace
    from agentloop.findings import build_diagnosis
    from agentloop.interventions import build_intervention

    baseline = _quickstart_trace()
    candidate = deepcopy(baseline)
    candidate.run_id = "candidate-original"
    for event in candidate.events:
        event.run_id = candidate.run_id
    findings = build_diagnosis(baseline)["findings"]
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[row["finding_id"] for row in findings],
        intervention_type="owned-example",
    )
    original = record.to_dict()
    report = write_cohort_study(
        cohort(tmp_path),
        tmp_path / "out",
        baseline="baseline",
        protocol=PROTOCOL,
        original_interventions=(record,),
    ).report()
    assert report["original_intervention_records"] == [original]
    assert record.to_dict() == original


def test_phase_metrics_and_synthetic_html_are_explicit(tmp_path):
    result = write_cohort_study(
        cohort(tmp_path), tmp_path / "out", baseline="baseline", protocol=PROTOCOL
    )
    report = result.report()
    row = report["conditions"]["baseline"]["trial_rows"][0]
    assert row["metrics"]["runtime_ms"] == 4000
    assert row["metrics"]["trial_runtime_ms"] == 5000
    assert row["metrics"]["model_call_count"] == 1
    assert row["measurement_provenance"]["provider_billing_verified"] is False
    html = (tmp_path / "out/cohort-report.html").read_text()
    assert "Synthetic inputs" in html and "causal harness effect not established" in html
