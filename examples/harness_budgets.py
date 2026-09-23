"""Demonstrate budget admission with synthetic usage and no external services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig, HarnessDeniedError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harness-budget.json"))
    out = parser.parse_args().out
    calls = []

    def model():
        calls.append(True)
        return {"text": "synthetic result", "tokens": 8, "cost": 0.002}

    def read_usage(reply):
        return ResourceUsage(
            tokens=reply["tokens"],
            cost_usd=reply["cost"],
            token_provenance="user_supplied",
            cost_provenance="user_reported",
            complete=True,
        )

    policy = budget_policy(
        BudgetLimits(max_model_calls=2, max_tokens=20, max_cost_usd=0.006), soft_fraction=0.5
    )
    with trace_agent("synthetic-budget", metadata={"synthetic": True}) as trace:
        run = Harness(HarnessConfig("enforce", (policy,))).start_run()
        wrapped = run.wrap(
            model,
            boundary="model",
            dispatch=DispatchOptions(
                Reservation(10, 0.003, "upper_bound", True, ("synthetic-pricing-v1",))
            ),
            usage_reader=read_usage,
        )
        wrapped()
        wrapped()
        try:
            wrapped()
        except HarnessDeniedError:
            print("Third call denied before dispatch.")
        else:
            raise RuntimeError("The budget did not deny the third call")
    if len(calls) != 2:
        raise RuntimeError("Unexpected number of dispatched calls")
    trace.export_json(out)
    latest = run.results[-1].proposals[0].decision.budget_snapshot.to_dict()
    print(json.dumps(latest["total_usage"], sort_keys=True))
    print(f"Wrote {out}; synthetic accounting, not a provider billing guarantee.")


if __name__ == "__main__":
    main()
