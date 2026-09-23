"""Optional real-runtime enforcement tests. No provider clients or credentials."""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import (
    ACTIONS,
    Decision,
    HarnessConfig,
    HarnessControlError,
    HarnessDeniedError,
    HarnessStoppedError,
    Hook,
    Policy,
)
from agentloop.integrations.langgraph import trace_node
from agentloop.integrations.langgraph_harness import LangGraphHarness
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint
from agentloop.tracer import current_event_id, current_trace

langgraph = pytest.importorskip("langgraph.graph")
from langgraph.runtime import Runtime  # noqa: E402
from langgraph.types import RetryPolicy  # noqa: E402


class State(TypedDict):
    value: int


def compile_node(node, **kwargs):
    builder = langgraph.StateGraph(State)
    builder.add_node("work", node, **kwargs)
    builder.set_entry_point("work")
    builder.set_finish_point("work")
    return builder.compile()


def denial(boundary):
    return Policy(
        "deny",
        "1",
        lambda ctx: Decision("deny", "test_denial"),
        hooks=frozenset({Hook(boundary)}),
        actions=ACTIONS,
    )


@pytest.mark.parametrize("entry", ["invoke", "ainvoke", "stream", "astream"])
@pytest.mark.parametrize("mode", ["disabled", "shadow", "enforce"])
def test_real_graph_state_trace_parent_and_completion(entry, mode):
    adapter = LangGraphHarness(
        HarnessConfig(mode), boundaries={"iteration", "model", "tool", "completion"}
    )
    calls = []

    def model(value):
        calls.append(("model", current_event_id()))
        return value + 2

    model = adapter.model(model)
    tool = adapter.tool(lambda value: value * 3)

    @trace_node("work")
    @adapter.node("work")
    def node(state: State, runtime: Runtime):
        assert runtime.execution_info.node_attempt == 1
        return {"value": tool(model(state["value"]))}

    app = adapter.runnable(compile_node(node))
    with trace_agent("graph") as trace:
        if entry == "invoke":
            result = app.invoke({"value": 1})
            run = adapter.last_run
        elif entry == "stream":
            source = app.stream({"value": 1})
            assert calls == []
            result = list(source)
            run = adapter.last_run
        else:

            async def execute():
                if entry == "ainvoke":
                    result = await app.ainvoke({"value": 1})
                else:
                    source = app.astream({"value": 1})
                    assert calls == []
                    result = [item async for item in source]
                return result, adapter.last_run

            result, run = asyncio.run(execute())
        assert current_trace() is trace and current_event_id() is None
    assert result == ([{"work": {"value": 9}}] if "stream" in entry else {"value": 9})
    assert len(trace.events) == 1
    assert calls == [("model", trace.events[0].event_id)]
    if mode != "disabled":
        assert len(run.results) == 8
        assert {r.parent_span_id for r in run.results if r.hook.boundary != "completion"} == {
            trace.events[0].event_id
        }
        assert {r.trace_id for r in run.results} == {trace.run_id}


@pytest.mark.parametrize("boundary", ["iteration", "model", "tool"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_denial_bypasses_custom_retry_and_error_handler(boundary, asynchronous):
    adapter = LangGraphHarness(
        HarnessConfig("enforce", (denial(boundary),)), boundaries={"iteration", "model", "tool"}
    )
    calls = []

    def work(value):
        calls.append("protected")
        return value

    protected = getattr(adapter, boundary)(work) if boundary != "iteration" else work

    @adapter.node("work")
    def node(state: State):
        return {"value": protected(state["value"])}

    @adapter.node("work")
    async def anode(state: State):
        return {"value": protected(state["value"])}

    def retry(error):
        calls.append("retry")
        return True

    def handler(state, error):
        calls.append("handler")
        return state

    app = adapter.runnable(
        compile_node(
            anode if asynchronous else node,
            retry_policy=RetryPolicy(
                max_attempts=2, initial_interval=0, jitter=False, retry_on=retry
            ),
            error_handler=handler,
        )
    )
    with pytest.raises(HarnessDeniedError):
        if asynchronous:
            asyncio.run(app.ainvoke({"value": 1}))
        else:
            app.invoke({"value": 1})
    assert calls == []


@pytest.mark.parametrize("guard", ["budget", "loop"])
def test_budget_and_loop_deny_before_fake_provider(guard):
    policy = (
        budget_policy(BudgetLimits(max_model_calls=1), boundaries={"model"})
        if guard == "budget"
        else loop_guard_policy(LoopLimits(max_identical_calls=1), boundaries={"model"})
    )
    adapter = LangGraphHarness(
        HarnessConfig("enforce", (policy,)), boundaries={"iteration", "model"}
    )
    calls = []

    def model(value):
        calls.append(value)
        return value

    model = adapter.model(
        model,
        dispatch=DispatchOptions(
            step=StepInfo(
                "model", fingerprint({"input": 1}), fingerprint({"progress": 0}), mutating=False
            )
        ),
    )

    @adapter.node("work")
    def node(state: State):
        model(state["value"])
        return {"value": model(state["value"])}

    with pytest.raises(HarnessControlError) as caught:
        adapter.runnable(compile_node(node)).invoke({"value": 1})
    assert caught.value.result.action == ("deny" if guard == "budget" else "stop")
    assert calls == [1]


def test_real_retries_have_stable_branch_and_framework_origin():
    contexts = []

    def observe(ctx):
        contexts.append(ctx)
        return Decision()

    policy = Policy("observe", "1", observe, hooks=frozenset({Hook("iteration")}))
    adapter = LangGraphHarness(HarnessConfig("shadow", (policy,)))
    attempts = []

    @adapter.node("work", step_reader=lambda state: StepInfo("work", mutating=False))
    def node(state: State):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("transient fake failure")
        return state

    app = adapter.runnable(
        compile_node(
            node,
            retry_policy=RetryPolicy(
                max_attempts=2, initial_interval=0, jitter=False, retry_on=OSError
            ),
        )
    )
    assert app.invoke({"value": 1}) == {"value": 1}
    assert len(contexts) == 2
    assert contexts[0].branch_id == contexts[1].branch_id
    assert [ctx.dispatch.retry_source for ctx in contexts] == [None, "framework"]


def test_stable_branch_enables_node_loop_guard():
    policy = loop_guard_policy(LoopLimits(max_identical_calls=1), boundaries={"iteration"})
    adapter = LangGraphHarness(HarnessConfig("enforce", (policy,)), boundaries={"iteration"})
    calls = []

    @adapter.node(
        "work",
        branch_id="main",
        step_reader=lambda state: StepInfo(
            "work", fingerprint(state), fingerprint(state), mutating=False
        ),
    )
    def node(state: State):
        calls.append(True)
        return state

    builder = langgraph.StateGraph(State)
    builder.add_node("work", node)
    builder.set_entry_point("work")
    builder.add_edge("work", "work")
    with pytest.raises(HarnessStoppedError):
        adapter.runnable(builder.compile()).invoke({"value": 1})
    assert calls == [True]


def test_original_async_failure_and_native_self_cancellation():
    async def scenario():
        from langgraph.errors import NodeCancelledError

        for error in (ValueError("original"), asyncio.CancelledError("cancel")):
            adapter = LangGraphHarness(HarnessConfig("enforce"))

            @adapter.node("work")
            async def node(state: State):
                raise error

            app = adapter.runnable(compile_node(node))
            expected = (
                NodeCancelledError if isinstance(error, asyncio.CancelledError) else type(error)
            )
            with pytest.raises(expected) as caught:
                await app.ainvoke({"value": 1})
            if isinstance(error, asyncio.CancelledError):
                assert caught.value.__cause__ is error
                with pytest.raises(NodeCancelledError) as baseline:
                    await compile_node(node.__wrapped__).ainvoke({"value": 1})
                assert baseline.value.__cause__ is error
            else:
                assert caught.value is error
            assert not any(r.hook.boundary == "completion" for r in adapter.last_run.results)

    asyncio.run(scenario())


def test_external_cancellation_cleans_up_and_restores_context():
    async def scenario():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        adapter = LangGraphHarness(HarnessConfig("enforce"))

        @adapter.node("work")
        async def node(state: State):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        app = adapter.runnable(compile_node(node))
        runs = []

        async def execute():
            try:
                await app.ainvoke({"value": 1})
            finally:
                runs.append(adapter.last_run)

        with trace_agent("cancel") as trace:
            task = asyncio.create_task(execute())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert current_trace() is trace and current_event_id() is None
        assert cleaned.is_set()
        assert runs[0].results[-1].status == "cancelled"
        assert not any(r.hook.boundary == "completion" for r in runs[0].results)

    asyncio.run(scenario())


def test_completion_denial_happens_after_graph_work():
    adapter = LangGraphHarness(HarnessConfig("enforce", (denial("completion"),)))
    calls = []

    @adapter.node("work")
    def node(state: State):
        calls.append(state)
        return state

    with pytest.raises(HarnessDeniedError) as caught:
        adapter.runnable(compile_node(node)).invoke({"value": 1})
    assert caught.value.result.hook.boundary == "completion"
    assert calls == [{"value": 1}]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_streaming_model_inside_real_node_is_lazy_and_controlled(asynchronous):
    adapter = LangGraphHarness(
        HarnessConfig("enforce", (denial("model"),)), boundaries={"iteration", "model"}
    )
    calls = []

    @adapter.model
    def model():
        calls.append(True)
        yield 1

    @adapter.model
    async def amodel():
        calls.append(True)
        yield 1

    @adapter.node("work")
    def node(state: State):
        chunks = model()
        assert not calls
        return {"value": sum(chunks)}

    @adapter.node("work")
    async def anode(state: State):
        chunks = amodel()
        assert not calls
        return {"value": sum([value async for value in chunks])}

    app = adapter.runnable(compile_node(anode if asynchronous else node))
    with pytest.raises(HarnessDeniedError):
        if asynchronous:
            asyncio.run(app.ainvoke({"value": 0}))
        else:
            app.invoke({"value": 0})
    assert calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_early_stream_close_does_not_complete(asynchronous):
    adapter = LangGraphHarness(HarnessConfig("enforce"))

    @adapter.node("work")
    def node(state: State):
        return state

    app = adapter.runnable(compile_node(node))
    if asynchronous:

        async def consume():
            source = app.astream({"value": 1})
            assert await source.__anext__() == {"work": {"value": 1}}
            await source.aclose()
            return adapter.last_run

        run = asyncio.run(consume())
    else:
        source = app.stream({"value": 1})
        assert next(source) == {"work": {"value": 1}}
        source.close()
        run = adapter.last_run
    assert not any(r.hook.boundary == "completion" for r in run.results)
