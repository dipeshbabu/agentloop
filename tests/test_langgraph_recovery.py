"""Pinned SDK recovery path; no network, models or production side effects."""

from __future__ import annotations

from typing import TypedDict

import pytest

from agentloop import trace_agent
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.checkpoint_state import CheckpointError
from agentloop.checkpoint_store import MemoryRecoveryStore
from agentloop.harness import HarnessConfig, HarnessControlError
from agentloop.integrations.langgraph_harness import LangGraphHarness
from agentloop.integrations.langgraph_recovery import LangGraphRecovery
from agentloop.recovery_evidence import recovery_status

graph_module = pytest.importorskip("langgraph.graph")
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402


class State(TypedDict):
    value: int


def build(*, limit=2, interrupt=True, safe=True, failure=False, retries=None):
    calls = []
    adapter = LangGraphHarness(
        HarnessConfig(
            "enforce", (budget_policy(BudgetLimits(max_model_calls=limit, max_retries=retries)),)
        ),
        boundaries={"iteration", "model", "tool", "completion"},
    )

    def model():
        calls.append(True)
        return 1

    model = adapter.model(model)

    @adapter.node("first")
    def first(state: State):
        return {"value": state["value"] + model()}

    failure_flag = [failure]

    @adapter.node("second")
    def second(state: State):
        if failure_flag[0]:
            failure_flag[0] = False
            raise RuntimeError("synthetic interruption")
        return {"value": state["value"] + model()}

    builder = graph_module.StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(graph_module.START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", graph_module.END)
    compiled = builder.compile(
        checkpointer=InMemorySaver(), interrupt_before=["second"] if interrupt else None
    )
    controlled = adapter.runnable(compiled)
    store = MemoryRecoveryStore()
    recovery = LangGraphRecovery(
        controlled,
        store,
        owner="example",
        thread_id="thread",
        graph_version="test.v1",
        clock_domain="test-process",
        retry_safe_nodes={"second"} if safe else (),
    )
    return controlled, recovery, calls


def test_real_checkpoint_resumes_only_remaining_node_and_preserves_budget():
    controlled, recovery, calls = build()
    with trace_agent("paused") as first:
        assert (
            controlled.invoke(
                {"value": 0}, {"configurable": {"thread_id": "thread"}}, durability="sync"
            )["value"]
            == 1
        )
        original = controlled._adapter.last_run
        reference = recovery.capture()
    assert recovery_status(first) == "incomplete"
    with trace_agent("resumed") as second:
        result = recovery.resume(reference, request_id="resume")
    assert result.value == {"value": 2}
    assert result.status == "completed" and recovery_status(second) == "completed"
    assert len(calls) == 2 and result.run_id != original.run_id
    snapshots = [
        proposal.decision.budget_snapshot.to_dict()
        for hook in recovery.last_run.results
        for proposal in hook.proposals
        if proposal.decision.budget_snapshot is not None
    ]
    assert snapshots[-1]["committed"]["model_calls"] == 2


def test_real_resume_cannot_reset_model_budget():
    controlled, recovery, calls = build(limit=1)
    controlled.invoke({"value": 0}, {"configurable": {"thread_id": "thread"}}, durability="sync")
    reference = recovery.capture()
    with pytest.raises(HarnessControlError):
        recovery.resume(reference, request_id="resume")
    assert len(calls) == 1


def test_real_failed_node_requires_explicit_retry_safety_and_does_not_repeat_first():
    controlled, recovery, calls = build(interrupt=False, failure=True)
    with pytest.raises(RuntimeError, match="synthetic"):
        controlled.invoke(
            {"value": 0}, {"configurable": {"thread_id": "thread"}}, durability="sync"
        )
    reference = recovery.capture()
    assert recovery.resume(reference, request_id="resume").value == {"value": 2}
    assert len(calls) == 2

    controlled, recovery, calls = build(safe=False, interrupt=False, failure=True)
    with pytest.raises(RuntimeError, match="synthetic"):
        controlled.invoke(
            {"value": 0}, {"configurable": {"thread_id": "thread"}}, durability="sync"
        )
    reference = recovery.capture()
    with pytest.raises(CheckpointError, match="side_effect_reconciliation"):
        recovery.resume(reference, request_id="unsafe")
    assert len(calls) == 1


def test_real_recovery_attempt_respects_harness_retry_budget():
    controlled, recovery, calls = build(retries=0)
    controlled.invoke({"value": 0}, {"configurable": {"thread_id": "thread"}}, durability="sync")
    reference = recovery.capture()
    with pytest.raises(HarnessControlError):
        recovery.resume(reference, request_id="no-more-retries")
    assert len(calls) == 1
