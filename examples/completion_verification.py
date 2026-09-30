"""Offline completion acceptance, bounded repair, budget exhaustion and escalation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.completion import CompletionGate, completion_policy
from agentloop.completion_types import (
    CompletionCandidate,
    CompletionConfig,
    RepairBackend,
    quality_check,
)
from agentloop.harness import Harness, HarnessConfig, HarnessControlError
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo


def run_example():
    results = {}
    traces = {}
    for name in ("accept", "repair", "exhausted_budget", "escalate"):
        repairs = 1 if name in {"repair", "exhausted_budget"} else 0
        config = CompletionConfig(
            "completion",
            "1",
            "finish",
            (quality_check("answer", "1", {"expected": {"answer": 42}}),),
            max_repairs=repairs,
            on_failure="escalate" if name == "escalate" else "stop",
        )
        with trace_agent("synthetic-completion-" + name, metadata={"synthetic": True}) as trace:
            run = Harness(
                HarnessConfig(
                    "enforce",
                    (
                        completion_policy(config),
                        budget_policy(
                            BudgetLimits(
                                max_model_calls=0 if name == "exhausted_budget" else 1,
                                max_tool_calls=2,
                                max_retries=1,
                            )
                        ),
                        loop_guard_policy(LoopLimits(max_retries_per_step=1)),
                    ),
                )
            ).start_run()
            repair = (
                RepairBackend(
                    lambda candidate, feedback, context: CompletionCandidate({"answer": 42}),
                    dispatch=DispatchOptions(
                        step=StepInfo("repair", mutating=False, retry_safe=True)
                    ),
                )
                if repairs
                else None
            )
            gate = CompletionGate(run, config=config, repair=repair)
            candidate = CompletionCandidate(
                {"answer": 42} if name == "accept" else {"success": True}
            )
            try:
                result = gate.complete(candidate)
                results[name] = {
                    "outcome": result.status,
                    "repairs": result.repairs,
                    "configured_checks_passed": result.verified,
                }
            except HarnessControlError as exc:
                results[name] = {"outcome": exc.result.action, "configured_checks_passed": False}
        traces[name] = trace
    return results, traces


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/completion"))
    out = parser.parse_args().out
    results, traces = run_example()
    out.mkdir(parents=True, exist_ok=True)
    for name, trace in traces.items():
        trace.export_json(out / (name + ".json"))
    print(
        json.dumps(
            {"synthetic": True, "independent_task_evaluation": "not performed", "cases": results},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
