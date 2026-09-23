"""Exercise declared progress and repetition bounds without model services."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig, HarnessStoppedError
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harness-loops.json"))
    out = parser.parse_args().out
    calls = []

    def work():
        calls.append(True)
        return "synthetic result"

    with trace_agent("synthetic-loop-guard", metadata={"synthetic": True}) as trace:
        run = Harness(
            HarnessConfig(
                "enforce",
                (
                    budget_policy(BudgetLimits(max_iterations=10, max_retries=2)),
                    loop_guard_policy(LoopLimits(max_identical_calls=2)),
                ),
            )
        ).start_run()
        for argument, progress in ((1, 0), (2, 0), (2, 1), (2, 1), (2, 1)):
            step = StepInfo(
                "work",
                fingerprint({"input": argument}),
                fingerprint({"progress": progress}),
                mutating=False,
            )
            try:
                run.wrap(work, boundary="iteration", dispatch=DispatchOptions(step=step))()
            except HarnessStoppedError as stopped:
                print(
                    next(
                        proposal.decision.feedback
                        for proposal in stopped.result.proposals
                        if proposal.decision.action == "stop"
                    )
                )
                break
        else:
            raise RuntimeError("Expected the configured repetition bound to stop the fifth call")
    if len(calls) != 4:
        raise RuntimeError("Changed inputs/progress were not preserved")
    trace.export_json(out)
    print(f"Accepted {len(calls)} calls; wrote {out}. Synthetic evidence, not a usefulness claim.")


if __name__ == "__main__":
    main()
