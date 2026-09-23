"""Offline synthetic payment-review labels; no authorization or money movement."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop import WorkflowInfo, set_workflow_outcome, trace_workflow

if __package__:
    from .reference_support import decision, digest, write_reference_bundle
else:
    from reference_support import decision, digest, write_reference_bundle

FIXTURES = [
    {
        "id": "ordinary",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": True,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": False,
            "eligible": True,
            "manual_review": False,
            "route": "refund_review",
        },
    },
    {
        "id": "high-value-legitimate",
        "input": {
            "amount_band": "high",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": True,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": False,
            "eligible": True,
            "manual_review": False,
            "route": "refund_review",
        },
    },
    {
        "id": "small-duplicate",
        "input": {
            "amount_band": "low",
            "duplicate_signal": True,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": True,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": True,
            "eligible": None,
            "manual_review": True,
            "route": "manual_review",
        },
    },
    {
        "id": "missing-evidence",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": False,
            "within_fixture_window": None,
            "proof_present": None,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": None,
            "eligible": None,
            "manual_review": True,
            "route": "manual_review",
        },
    },
    {
        "id": "conflicting-evidence",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": True,
            "conflicting_evidence": True,
            "required_review": False,
        },
        "expected": {
            "suspicious": False,
            "eligible": None,
            "manual_review": True,
            "route": "manual_review",
        },
    },
    {
        "id": "outside-fixture-window",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": False,
            "proof_present": True,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": False,
            "eligible": False,
            "manual_review": False,
            "route": "standard_review",
        },
    },
    {
        "id": "required-review",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": True,
            "conflicting_evidence": False,
            "required_review": True,
        },
        "expected": {
            "suspicious": False,
            "eligible": True,
            "manual_review": True,
            "route": "manual_review",
        },
    },
    {
        "id": "missing-proof",
        "input": {
            "amount_band": "normal",
            "duplicate_signal": False,
            "evidence_complete": True,
            "within_fixture_window": True,
            "proof_present": False,
            "conflicting_evidence": False,
            "required_review": False,
        },
        "expected": {
            "suspicious": False,
            "eligible": False,
            "manual_review": False,
            "route": "standard_review",
        },
    },
]
VARIANTS = ("baseline", "hybrid", "cheap", "failing")


def decide(stage, payload, *, cheap=False):
    record, state = payload["record"], payload["state"]
    if stage == "suspicious":
        if cheap:
            return record["amount_band"] == "high"
        return bool(record["duplicate_signal"]) if record["evidence_complete"] else None
    if stage == "eligibility":
        if cheap:
            return bool(record["within_fixture_window"])
        if (
            not record["evidence_complete"]
            or record["conflicting_evidence"]
            or state["suspicious"] is not False
        ):
            return None
        return bool(record["within_fixture_window"] and record["proof_present"])
    if stage == "manual_review":
        return bool(
            state["suspicious"] is not False
            or state["eligible"] is None
            or record["required_review"]
            and not cheap
        )
    if stage == "route":
        if state["manual_review"]:
            return "manual_review"
        return "refund_review" if state["eligible"] else "standard_review"
    raise ValueError("unsupported fixture stage")


def run_payment(record, variant, task_id, fixture_hash):
    if variant not in VARIANTS:
        raise ValueError("unknown payment reference configuration")
    trace = None
    try:
        with trace_workflow(
            "payment-reference/" + variant,
            workflow=WorkflowInfo("reference.payment-review", "1.0"),
            task_id=task_id,
            metadata={
                "synthetic": True,
                "fixture_hash": fixture_hash,
                "variant": variant,
                "config_version": "1.0",
                "policy_basis": "fictional_evaluation_fixture",
            },
        ) as trace:
            state, previous = {}, []
            for stage, field in (
                ("suspicious", "suspicious"),
                ("eligibility", "eligible"),
                ("manual_review", "manual_review"),
                ("route", "route"),
            ):
                state[field] = decision(
                    trace,
                    stage,
                    {"record": record, "state": dict(state)},
                    lambda payload, stage=stage: decide(stage, payload, cheap=variant == "cheap"),
                    model_fixture=variant in {"baseline", "failing"}
                    or variant == "hybrid"
                    and stage == "suspicious",
                    depends_on=previous,
                    fail=variant == "failing" and task_id == "ordinary" and stage == "eligibility",
                )
                previous = [stage]
            trace.metadata["output"] = state
            set_workflow_outcome(
                trace, "review_labels_recorded", output_ref="sha256:" + digest(state)
            )
    except TimeoutError:
        if trace is None:
            raise
    return trace


def main(out):
    report = write_reference_bundle(
        out,
        name="Payment review reference",
        fixtures=FIXTURES,
        variants=VARIANTS,
        run_case=run_payment,
    )
    assert all(
        item["candidate_quality"] == 1
        for item in report["comparisons"]
        if item["variant"] == "hybrid"
    )
    assert any(
        item["candidate_quality"] < 1
        for item in report["comparisons"]
        if item["variant"] == "cheap"
    )
    assert any(
        item["candidate_quality"] is None
        for item in report["comparisons"]
        if item["variant"] == "failing"
    )
    print(f"Wrote synthetic payment-review comparisons to {out}; no payment action was performed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/payment-reference"))
    main(parser.parse_args().out)
