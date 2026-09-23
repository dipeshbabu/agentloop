"""Run with: uv run --with langgraph==1.2.11 python examples/langgraph_harness.py"""

from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import StateGraph

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import HarnessConfig, HarnessControlError
from agentloop.integrations.langgraph_harness import LangGraphHarness
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint


class State(TypedDict):
    value: int


def run(mode: str, guard: str) -> dict:
    policy = (
        budget_policy(BudgetLimits(max_model_calls=1), boundaries={"model"})
        if guard == "budget"
        else loop_guard_policy(LoopLimits(max_identical_calls=1), boundaries={"model"})
    )
    adapter = LangGraphHarness(
        HarnessConfig(mode, (policy,)),
        boundaries={"iteration", "model", "tool", "completion"},
    )
    calls = {"model": 0, "tool": 0}

    def fake_model(value):
        calls["model"] += 1
        return value + 1

    def fake_tool(value):
        calls["tool"] += 1
        return value * 2

    # Both calls deliberately use this same known offline input/state.
    model = adapter.model(
        fake_model,
        dispatch=DispatchOptions(
            step=StepInfo("lookup", fingerprint(2), fingerprint("unchanged"), mutating=False)
        ),
    )
    tool = adapter.tool(fake_tool)

    @adapter.node("work")
    def work(state: State):
        first = tool(model(state["value"]))
        second = tool(model(state["value"]))
        return {"value": first + second}

    builder = StateGraph(State)
    builder.add_node("work", work)
    builder.set_entry_point("work")
    builder.set_finish_point("work")
    app = adapter.runnable(builder.compile())
    with trace_agent(f"langgraph-{mode}-{guard}"):
        try:
            result = app.invoke({"value": 2})
            outcome = "returned"
        except HarnessControlError as exc:
            result = None
            outcome = exc.result.action
    expected = 1 if mode == "enforce" else 2
    assert calls == {"model": expected, "tool": expected}
    assert result == (None if mode == "enforce" else {"value": 12})
    if mode == "shadow":
        assert any(item.action != "continue" for item in adapter.last_run.results)
        assert all(not item.applied for item in adapter.last_run.results)
    return {"guard": guard, "mode": mode, "calls": calls, "outcome": outcome, "result": result}


if __name__ == "__main__":
    for guard in ("budget", "loop"):
        for mode in ("disabled", "shadow", "enforce"):
            print(json.dumps(run(mode, guard), sort_keys=True))
