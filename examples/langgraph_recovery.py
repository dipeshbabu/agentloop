"""Offline checkpoint resume with retained model quotas and distinct attempts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict

from agentloop import trace_agent
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.checkpoint_store import MemoryRecoveryStore
from agentloop.harness import HarnessConfig, HarnessControlError
from agentloop.integrations.langgraph_harness import LangGraphHarness
from agentloop.integrations.langgraph_recovery import LangGraphRecovery


def run_case(limit, out):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        value: int

    adapter = LangGraphHarness(
        HarnessConfig("enforce", (budget_policy(BudgetLimits(max_model_calls=limit)),)),
        boundaries={"iteration", "model", "tool", "completion"},
    )
    model = adapter.model(lambda: 1)  # Synthetic callback; no provider request.

    @adapter.node("first")
    def first(state: State):
        return {"value": state["value"] + model()}

    @adapter.node("second")
    def second(state: State):
        return {"value": state["value"] + model()}

    builder = StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    controlled = adapter.runnable(
        builder.compile(checkpointer=InMemorySaver(), interrupt_before=["second"])
    )
    recovery = LangGraphRecovery(
        controlled,
        MemoryRecoveryStore(),
        owner="synthetic-example",
        thread_id="example",
        graph_version="offline.v1",
        clock_domain="example-process",
        retry_safe_nodes={"second"},
    )
    with trace_agent("checkpoint-paused", metadata={"synthetic": True}) as paused:
        controlled.invoke(
            {"value": 0}, {"configurable": {"thread_id": "example"}}, durability="sync"
        )
        reference = recovery.capture()
    paused.export_json(out / "paused.json")
    with trace_agent("checkpoint-resumed", metadata={"synthetic": True}) as resumed:
        try:
            result = recovery.resume(reference, request_id="resume-once")
            status = result.status
        except HarnessControlError:
            status = "stopped"
    resumed.export_json(out / "resumed.json")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/checkpoint-recovery"))
    out = parser.parse_args().out
    results = {}
    for limit in (1, 2):
        folder = out / f"limit-{limit}"
        folder.mkdir(parents=True, exist_ok=True)
        results[str(limit)] = run_case(limit, folder)
    print(json.dumps({"synthetic": True, "durable": False, "outcomes": results}))


if __name__ == "__main__":
    main()
