"""Simulated evidence exercises gates; these fixtures are not empirical results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from agentloop import trace_agent
from agentloop.ablation_protocol import AblationProtocol
from agentloop.budget_types import ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.calibration import _digest
from agentloop.context_types import fingerprint
from agentloop.harness import Harness, HarnessConfig
from agentloop.policy_promotion import BudgetPromotion, CanaryOutcome
from agentloop.promotion_evidence import assess_promotion
from agentloop.promotion_types import (
    BudgetProposal,
    CanaryLimits,
    CapabilityEvidence,
    OperatorApproval,
    PromotionThresholds,
)

NOW = "2026-09-15T00:00:00Z"
EXPIRY = "2026-10-01T00:00:00Z"


def fixture_inputs():
    candidate = BudgetLimits(max_model_calls=2)
    policy = budget_policy(candidate)
    declaration = {policy.policy_id: policy.version + ":" + policy.config_hash}
    versions = {
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
    }
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
        "shadow": {"mode": "shadow", "policies": declaration},
        "enforce": {"mode": "enforce", "policies": declaration},
    }
    held_out = ("a", "b", *("task" + str(index) for index in range(18)))
    tasks = {
        name: {
            "split": "pilot" if name == "pilot" else "held_out",
            "input_sha256": fingerprint(name),
        }
        for name in ("pilot", *held_out)
    }
    spec = {
        "schema_version": "1.0",
        "name": "SIMULATED eligibility fixture",
        "workload_id": "fixture",
        "permission_ref": "unit-test",
        "frozen_at": "2026-09-01T00:00:00Z",
        "synthetic": False,
        "versions": versions,
        "tasks": tasks,
        "conditions": conditions,
        "schedule": [
            {
                "task_id": name,
                "repetition": "0",
                "cache_condition": "cold",
                "order": list(conditions),
            }
            for name in tasks
        ],
        "quality_gate": {"min_score": 0.95, "max_regression": 0},
        "bootstrap": {"samples": 200, "seed": 1, "confidence": 0.95},
    }
    protocol = AblationProtocol.freeze(spec)
    rows, evidence = [], {}
    usage = ResourceUsage(
        tokens=1,
        cost_usd=0.01,
        token_provenance="user_supplied",
        cost_provenance="provider_reported",
        complete=True,
    )
    for task in tasks:
        for position, (condition, settings) in enumerate(conditions.items()):
            trace_id = None
            if condition in {"shadow", "enforce"}:
                with trace_agent("simulated") as trace:
                    run = Harness(HarnessConfig(settings["mode"], (policy,))).start_run()
                    run.wrap(lambda: 42, boundary="model", usage_reader=lambda value: usage)()
                trace_id = trace.run_id
                evidence[trace_id] = run.export_evidence()
                # Align native decisions with this simulated fixture clock.
                for decision in evidence[trace_id]["decisions"].values():
                    decision["timing"]["started_at"] = "2026-09-02T00:00:00Z"
            elif condition == "trace":
                trace_id = "trace_" + task
            rows.append(
                {
                    "observation_id": task + "-" + condition,
                    "protocol_hash": protocol.protocol_hash,
                    "task_id": task,
                    "repetition": "0",
                    "cache_condition": "cold",
                    "condition": condition,
                    "position": position,
                    "started_at": "2026-09-02T00:00:00Z",
                    "versions": versions,
                    "reset_confirmed": True,
                    "provider_seed": 1,
                    "status": "completed",
                    "stop_reason": None,
                    "success": True,
                    "quality_score": 1,
                    "metrics": {
                        "latency_ms": {"off": 99, "trace": 100, "shadow": 101, "enforce": 80}[
                            condition
                        ],
                        "tokens": 1,
                        "cost_usd": 0.01,
                        "model_calls": 1,
                        "tool_calls": 0,
                        "retries": 0,
                        "policy_eval_ms": 1 if condition in {"shadow", "enforce"} else 0,
                    },
                    "token_status": "exact",
                    "cost_status": "complete",
                    "cost_basis": "provider_reported",
                    "trace_run_id": trace_id,
                }
            )
    context = {
        "policy": {
            "id": policy.policy_id,
            "version": policy.version,
            "config_hash": policy.config_hash,
        },
        "workload": "fixture",
        **{key: versions[key] for key in ("model", "provider", "environment", "scorer")},
        "quality_gate": spec["quality_gate"],
        "cost_basis": "provider_reported",
    }
    cohort_identity = {
        "context": context,
        "estimator": {
            "estimator_id": "fixture",
            "estimator_version": "1",
            "method": "simulated",
            "formula": "constant",
            "parameters": {},
        },
    }
    cohort_id = "cal_" + _digest(cohort_identity)
    calibration = {
        "schema_version": "1.0",
        "selection_inventory_complete": True,
        "validity": {"as_of": "2026-09-03T00:00:00Z", "valid_until": EXPIRY},
        "cohorts": [{"cohort_id": cohort_id, **cohort_identity}],
        "registrations": [
            {
                "cohort_id": cohort_id,
                "cohort": cohort_identity,
                "split": "held_out",
                "task_key": task,
                "synthetic": False,
                "selection": "selected",
                "issues": [],
                "metrics": {
                    "latency": {"signed_error": 1, "empirical_eligible": True},
                    "cost": {"signed_error": 0.001, "empirical_eligible": True},
                },
            }
            for task in held_out
        ],
    }
    calibration["artifact_hash"] = _digest(calibration)
    return {
        "baseline": BudgetLimits(),
        "candidate": candidate,
        "protocol": protocol,
        "observations": rows,
        "harness_evidence": evidence,
        "calibration": calibration,
        "calibration_cohort": cohort_id,
        "capability": CapabilityEvidence(
            "python",
            "fixture-v1",
            policy.config_hash,
            fingerprint("conformance-test-fixture"),
            "2026-09-03T00:00:00Z",
            True,
            4,
        ),
        "thresholds": PromotionThresholds(
            min_held_out_tasks=20, min_held_out_pairs=20, min_calibration_tasks=20
        ),
        "canary": CanaryLimits(2, 60, 120, 0.02),
        "valid_until": EXPIRY,
    }


@pytest.fixture
def inputs():
    return fixture_inputs()


def proposal(inputs):
    return BudgetProposal("fixture", **inputs)


def approval(value, **changes):
    return replace(
        OperatorApproval(
            value.proposal_hash, "operator", "review1", True, ("opted-in",), NOW, EXPIRY
        ),
        **changes,
    )


def controller(inputs):
    value = proposal(inputs)
    clock = [NOW]
    control = BudgetPromotion(value, baseline_review_ref="baseline-review", clock=lambda: clock[0])
    control.shadow_configuration()
    assert control.review(approval(value))["eligible"]
    control.begin_canary()
    return control, clock


def admit(control, inputs, **changes):
    return control.admit(
        scope="opted-in",
        versions=inputs["protocol"].to_dict()["specification"]["versions"],
        workload_id="fixture",
        opted_in=True,
        **changes,
    )


def test_eligibility_is_owned_and_never_authorizes_activation(inputs):
    value = proposal(inputs)
    inputs["observations"].clear()
    result = assess_promotion(value, now=NOW)
    assert result["eligible"] and not result["enforcement_authorized"]
    assert result["diagnostics"]["decision_coverage"] == {"planned": 42, "verified": 42}


@pytest.mark.parametrize(
    "change,expected",
    [
        ("quality", "held_out_quality_ineligible"),
        ("cost", "cost_evidence_missing"),
        ("failed", "held_out_quality_ineligible"),
        ("latency", "performance_threshold_failed"),
        ("model", "incomplete_or_mismatched_observations"),
        ("missing", "incomplete_or_mismatched_observations"),
        ("coverage", "decision_coverage_incomplete"),
        ("small", "held_out_sample_too_small"),
        ("synthetic", "synthetic_evidence"),
        ("calibration", "invalid_or_missing_calibration"),
        ("shadow", "shadow_overhead_exceeded"),
    ],
)
def test_missing_failed_synthetic_shifted_and_small_evidence_is_ineligible(
    inputs, change, expected
):
    row = next(row for row in inputs["observations"] if row["observation_id"] == "a-enforce")
    if change == "quality":
        row["quality_score"] = None
    elif change == "cost":
        row["cost_status"], row["metrics"]["cost_usd"] = "unknown", None
    elif change == "failed":
        row["status"], row["success"] = "failed", False
    elif change == "latency":
        row["metrics"]["latency_ms"] = 2000
    elif change == "model":
        row["versions"] = {**row["versions"], "model": "changed"}
    elif change == "missing":
        inputs["observations"].remove(row)
    elif change == "coverage":
        inputs["harness_evidence"].clear()
    elif change == "small":
        inputs["thresholds"] = PromotionThresholds()
    elif change == "synthetic":
        spec = inputs["protocol"].to_dict()["specification"]
        spec["synthetic"] = True
        inputs["protocol"] = AblationProtocol.freeze(spec)
        for row in inputs["observations"]:
            row["protocol_hash"] = inputs["protocol"].protocol_hash
    elif change == "calibration":
        inputs["calibration"] = None
    else:
        for row in inputs["observations"]:
            if row["condition"] == "shadow":
                row["metrics"]["latency_ms"] = 200
    result = assess_promotion(proposal(inputs), now=NOW)
    assert not result["eligible"] and expected in result["reasons"]


def test_expired_and_changed_calibration_context_reject(inputs):
    assert not assess_promotion(proposal(inputs), now="2026-10-02T00:00:00Z")["eligible"]
    calibration = inputs["calibration"]
    calibration["cohorts"][0]["context"]["workload"] = "shifted"
    calibration["validity"]["valid_until"] = "2026-09-10T00:00:00Z"
    calibration["artifact_hash"] = _digest(
        {key: value for key, value in calibration.items() if key != "artifact_hash"}
    )
    reasons = assess_promotion(proposal(inputs), now=NOW)["reasons"]
    assert "calibration_context_mismatch" in reasons and "calibration_expired_or_future" in reasons


def test_operator_rejection_and_missing_shadow_transition_never_enforce(inputs):
    value = proposal(inputs)
    control = BudgetPromotion(value, baseline_review_ref="baseline", clock=lambda: NOW)
    with pytest.raises(ValueError, match="shadow"):
        control.review(approval(value))
    control.shadow_configuration()
    control.review(approval(value, approved=False))
    assert control.state == "rejected" and not admit(control, inputs).candidate
    with pytest.raises(ValueError, match="approval"):
        control.begin_canary()


def test_scope_opt_in_and_changed_runtime_versions(inputs):
    control, _ = controller(inputs)
    versions = inputs["protocol"].to_dict()["specification"]["versions"]
    assert not control.admit(scope="opted-in", versions=versions, workload_id="fixture").candidate
    assert not control.admit(
        scope="outside", versions=versions, workload_id="fixture", opted_in=True
    ).candidate
    assert not control.admit(
        scope="opted-in",
        versions={**versions, "model": "changed"},
        workload_id="fixture",
        opted_in=True,
    ).candidate
    assert control.state == "rolled_back"


@pytest.mark.parametrize(
    "bad",
    [
        CanaryOutcome("failed", 1, 1, 0, True),
        CanaryOutcome("completed", None, 1, 0, True),
        CanaryOutcome("completed", 1, None, 0, True),
        CanaryOutcome("completed", 1, 1, None, False),
        CanaryOutcome("completed", 0.5, 1, 0, True),
        CanaryOutcome("completed", 1, 1000, 0, True),
        CanaryOutcome("completed", 1, 1, 1, True),
    ],
)
def test_canary_regression_or_unknown_measurement_restores_reviewed_baseline(inputs, bad):
    control, _ = controller(inputs)
    lease = admit(control, inputs)
    assert lease.candidate and lease.configuration.mode == "enforce"
    control.report(lease, bad)
    fallback = admit(control, inputs)
    assert control.state == "rolled_back" and fallback.configuration.mode == "disabled"
    assert control.export_evidence()["outcomes"][lease.lease_id]["status"] == bad.status


def test_admission_bound_preserves_all_outcomes_and_never_expands_after_passing(inputs):
    control, _ = controller(inputs)
    one, two = admit(control, inputs), admit(control, inputs)
    assert not admit(control, inputs).candidate
    outcome = CanaryOutcome("completed", 1, 100, 0.01, True)
    control.report(one, outcome)
    control.report(two, outcome)
    assert control.state == "canary_passed"
    assert not admit(control, inputs).candidate
    assert control.export_evidence()["missing"] == 0
    with pytest.raises(ValueError, match="already reported"):
        control.report(one, outcome)


@pytest.mark.parametrize("stop", ["kill", "deadline", "expiry", "backwards", "manual"])
def test_kill_timeout_expiry_clock_and_operator_rollback_keep_missing_denominator(inputs, stop):
    control, clock = controller(inputs)
    admit(control, inputs)
    if stop == "kill":
        control.kill_switch.set()
    elif stop == "deadline":
        clock[0] = "2026-09-15T00:01:01Z"
    elif stop == "expiry":
        clock[0] = "2026-10-01T00:00:00Z"
    elif stop == "backwards":
        clock[0] = "2026-09-14T00:00:00Z"
    else:
        control.rollback()
    if stop == "backwards":
        with pytest.raises(ValueError, match="backwards"):
            control.tick()
    else:
        control.tick()
    assert control.state == "rolled_back"
    assert control.export_evidence()["missing"] == 1


def test_native_decisions_and_failed_outcomes_are_retained(inputs):
    with trace_agent("promotion") as trace:
        control, _ = controller(inputs)
        lease = admit(control, inputs)
        control.report(lease, CanaryOutcome("stopped", None, 1, None, False))
    assert "agentloop.harness" in trace.metadata
    records = trace.metadata["agentloop.policy_promotion"]["records"]
    assert any(row["reason"] == "canary_approved" for row in records)
    assert any(row["reason"] == "canary_outcome_failed" for row in records)
    assert records[-1]["details"]["rollback_config_hash"] == HarnessConfig("disabled").config_hash


def test_proposal_hash_binds_candidate_thresholds_and_canary(inputs):
    original = proposal(inputs).proposal_hash
    changed = deepcopy(inputs)
    changed["canary"] = replace(inputs["canary"], max_admissions=3)
    assert proposal(changed).proposal_hash != original
    changed = deepcopy(inputs)
    changed["thresholds"] = replace(inputs["thresholds"], max_cost_regression_usd=0.001)
    assert proposal(changed).proposal_hash != original


@pytest.mark.parametrize("change", ["hash", "future", "expired", "before_evidence"])
def test_approval_binds_exact_proposal_and_review_window(inputs, change):
    value = proposal(inputs)
    control = BudgetPromotion(value, baseline_review_ref="baseline", clock=lambda: NOW)
    control.shadow_configuration()
    changes = {
        "hash": {"proposal_hash": fingerprint("different")},
        "future": {"approved_at": "2026-09-16T00:00:00Z"},
        "expired": {"approved_at": "2026-09-03T00:00:00Z", "valid_until": "2026-09-14T00:00:00Z"},
        "before_evidence": {"approved_at": "2026-09-01T00:00:00Z"},
    }
    assessment = control.review(approval(value, **changes[change]))
    assert not assessment["operator_approved"] and assessment["approval_reasons"]
    assert control.state == "rejected" and not admit(control, inputs).candidate


def test_workload_shift_is_checked_separately_from_model_versions(inputs):
    control, _ = controller(inputs)
    result = control.admit(
        scope="opted-in",
        versions=inputs["protocol"].to_dict()["specification"]["versions"],
        workload_id="new-workload",
        opted_in=True,
    )
    assert not result.candidate and control.state == "rolled_back"


def test_hard_sample_floor_cannot_be_disabled():
    with pytest.raises(ValueError, match="independent"):
        PromotionThresholds(min_held_out_tasks=2)
    with pytest.raises(ValueError, match="independent"):
        PromotionThresholds(min_calibration_tasks=2)


def test_concurrent_admission_respects_bound_and_native_export_survives_without_trace(inputs):
    from concurrent.futures import ThreadPoolExecutor

    from agentloop.harness_evidence import validate_evidence

    control, _ = controller(inputs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda unused: admit(control, inputs), range(16)))
    assert sum(lease.candidate for lease in leases) == 2
    evidence = control.export_evidence()
    assert validate_evidence(evidence["native_evidence"])["decisions"]
    assert evidence["missing"] == 2


def test_corrupt_trace_evidence_blocks_canary_and_rolls_back(inputs):
    control, _ = controller(inputs)
    with trace_agent("broken", metadata={"agentloop.harness": {}}), pytest.raises(ValueError):
        admit(control, inputs)
    assert control.state == "rolled_back"


def test_capability_expiry_during_canary_forces_rollback(inputs):
    inputs["capability"] = replace(inputs["capability"], tested_at="2026-08-16T00:00:01Z")
    control, clock = controller(inputs)
    clock[0] = "2026-09-15T00:00:02Z"
    assert control.tick() == "rolled_back"


def test_invalid_clock_stops_future_admission(inputs):
    control, clock = controller(inputs)
    clock[0] = "invalid"
    with pytest.raises(ValueError):
        control.tick()
    assert control.state == "rolled_back"


def test_review_of_ineligible_missing_calibration_returns_reasons(inputs):
    inputs["calibration"] = None
    value = proposal(inputs)
    control = BudgetPromotion(value, baseline_review_ref="baseline", clock=lambda: NOW)
    control.shadow_configuration()
    result = control.review(approval(value))
    assert result["state"] == "rejected" and not result["eligible"]
    assert "invalid_or_missing_calibration" in result["reasons"]
