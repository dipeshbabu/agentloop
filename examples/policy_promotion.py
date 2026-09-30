"""Show why synthetic ablation fixtures cannot authorize budget enforcement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_types import fingerprint
from agentloop.promotion_evidence import assess_promotion
from agentloop.promotion_types import BudgetProposal, CanaryLimits, CapabilityEvidence
from examples.harness_ablation import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/policy-promotion"))
    out = parser.parse_args().out
    run(out / "synthetic-fixture")
    bundle = json.loads((out / "synthetic-fixture" / "bundle.json").read_text())
    protocol = json.loads((out / "synthetic-fixture" / "protocol.json").read_text())
    candidate = BudgetLimits(max_model_calls=1)
    proposal = BudgetProposal(
        "synthetic-example",
        baseline=BudgetLimits(),
        candidate=candidate,
        protocol=protocol,
        observations=bundle["observations"],
        calibration=None,
        calibration_cohort="not-supplied",
        capability=CapabilityEvidence(
            "python",
            "fixture-v1",
            budget_policy(candidate).config_hash,
            fingerprint("unverified-demo"),
            "2026-01-01T00:00:00Z",
            False,
            0,
        ),
        canary=CanaryLimits(2, 30, 1000, 0.01),
        valid_until="2026-01-31T00:00:00Z",
    )
    assessment = assess_promotion(proposal, now="2026-01-10T00:00:00Z")
    if assessment["eligible"]:
        raise RuntimeError("synthetic fixtures must remain ineligible")
    (out / "eligibility.json").write_text(json.dumps(assessment, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"synthetic": True, "enforcement_authorized": False, "reasons": assessment["reasons"]}
        )
    )


if __name__ == "__main__":
    main()
