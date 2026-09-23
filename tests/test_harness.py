from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from functools import partial
from threading import Barrier

import pytest

from agentloop import current_trace, trace_agent, trace_tool
from agentloop.harness import (
    ACTIONS,
    ALL_HOOKS,
    EXECUTION_KINDS,
    AdapterCapabilities,
    Decision,
    Harness,
    HarnessConfig,
    HarnessDeniedError,
    HarnessEscalationError,
    HarnessStoppedError,
    Hook,
    Policy,
)
from agentloop.tracer import current_event_id, trace_tool_call


def policy(callback, *, hooks=None, actions=ACTIONS, **kwargs):
    return Policy(
        "guard", "1", callback, hooks=frozenset(hooks or [Hook("model")]), actions=actions, **kwargs
    )


def run_with(callback, *, mode="enforce", hooks=None, actions=ACTIONS):
    return Harness(
        HarnessConfig(mode, (policy(callback, hooks=hooks, actions=actions),))
    ).start_run()


def callables(dispatched):
    def sync():
        dispatched.append(True)
        return 1

    async def asynchronous():
        dispatched.append(True)
        return 1

    def generator():
        dispatched.append(True)
        yield 1

    async def async_generator():
        dispatched.append(True)
        yield 1

    return {
        "sync": sync,
        "async": asynchronous,
        "generator": generator,
        "async_generator": async_generator,
    }


@pytest.mark.parametrize("kind", sorted(EXECUTION_KINDS))
def test_disabled_returns_original_callable_without_invoking_policies(kind):
    run = run_with(lambda context: pytest.fail("disabled policy ran"), mode="disabled")
    function = callables([])[kind]
    assert run.wrap(function, boundary="model") is function
    assert run.results == ()
    assert not run.stopped


@pytest.mark.parametrize("boundary", ["model", "tool", "iteration", "completion"])
@pytest.mark.parametrize("kind", sorted(EXECUTION_KINDS))
def test_enforced_denial_never_dispatches_supported_callables(boundary, kind):
    dispatched = []
    run = run_with(lambda context: Decision("deny", "denied"), hooks=[Hook(boundary)])
    protected = run.wrap(callables(dispatched)[kind], boundary=boundary)
    with pytest.raises(HarnessDeniedError):
        if kind == "sync":
            protected()
        elif kind == "async":
            asyncio.run(protected())
        elif kind == "generator":
            next(protected())
        else:
            asyncio.run(protected().__anext__())
    assert dispatched == []
    assert [(item.hook.phase, item.status) for item in run.results] == [
        ("before", "pending"),
        ("after", "denied"),
    ]
    assert not run.stopped


@pytest.mark.parametrize("action", ["deny", "stop", "escalate"])
def test_shadow_records_proposals_without_changing_results_or_stopping(action):
    run = run_with(lambda context: Decision(action), mode="shadow")
    output = object()
    protected = run.wrap(lambda: output, boundary="model")
    assert protected() is output and protected() is output
    assert not run.stopped
    assert run.results[0].action == action
    assert all(not result.applied for result in run.results)


def test_configuration_is_deeply_immutable_and_hashes_canonical_settings():
    configuration = {"nested": {"values": [1, 2]}, "enabled": True}
    first = policy(lambda context: Decision(), configuration=configuration)
    same = policy(
        lambda context: Decision(), configuration={"enabled": True, "nested": {"values": [1, 2]}}
    )
    configuration["nested"]["values"].append(3)
    assert first.config_hash == same.config_hash
    assert first.configuration["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        first.configuration["nested"]["other"] = True
    with pytest.raises(FrozenInstanceError):
        first.version = "2"
    assert (
        HarnessConfig("shadow", (first,)).config_hash
        != HarnessConfig("enforce", (first,)).config_hash
    )
    assert (
        policy(lambda context: Decision(), configuration={"enabled": False}).config_hash
        != first.config_hash
    )


@pytest.mark.parametrize(
    "configuration", [{"bad": float("nan")}, {"bad": float("inf")}, {1: "bad"}, {"bad": object()}]
)
def test_invalid_configuration_is_rejected(configuration):
    with pytest.raises(ValueError):
        policy(lambda context: Decision(), configuration=configuration)


def test_conflicts_use_deterministic_priority_then_id_and_strongest_action():
    order = []

    def callback(name, action):
        def evaluate(context):
            order.append(name)
            return Decision(action)

        return evaluate

    policies = (
        Policy("z", "1", callback("z", "continue"), priority=2),
        Policy("b", "1", callback("b", "stop"), actions={"stop"}),
        Policy("a", "1", callback("a", "deny"), actions={"deny"}),
        Policy("first", "1", callback("first", "escalate"), priority=-1, actions={"escalate"}),
    )
    config = HarnessConfig("enforce", policies)
    assert config.config_hash == HarnessConfig("enforce", tuple(reversed(policies))).config_hash
    run = Harness(config).start_run()
    with pytest.raises(HarnessEscalationError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    assert order == ["first", "a", "b", "z"]
    assert run.stopped
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    assert order == ["first", "a", "b", "z"]


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_policy_errors_are_safe_and_fail_closed_only_in_enforce(mode):
    def broken(context):
        raise ValueError("private prompt and credential")

    run = run_with(broken, mode=mode)
    dispatched = []
    protected = run.wrap(lambda: dispatched.append(True), boundary="model")
    if mode == "enforce":
        with pytest.raises(HarnessEscalationError) as error:
            protected()
        assert "private" not in str(error.value)
        assert dispatched == []
    else:
        protected()
        assert dispatched == [True]
    assert run.results[0].proposals[0].failed
    assert "private" not in repr(run.results)


def test_undeclared_decision_is_a_policy_failure_not_permission_to_dispatch():
    run = run_with(lambda context: Decision("deny"), actions={"continue"})
    with pytest.raises(HarnessEscalationError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    assert run.results[0].proposals[0].failed


@pytest.mark.parametrize("failure", [ValueError("original"), asyncio.CancelledError("original")])
def test_after_hook_failure_preserves_original_exception_and_stops_new_work(failure):
    def after(context):
        raise RuntimeError("policy failure details")

    def fail():
        raise failure

    run = run_with(after, hooks=[Hook("tool", "after")], actions={"continue"})
    with pytest.raises(type(failure)) as error:
        run.wrap(fail, boundary="tool")()
    assert error.value is failure
    assert run.stopped and run.results[-1].proposals[0].failed
    assert run.results[-1].status == (
        "cancelled" if isinstance(failure, asyncio.CancelledError) else "error"
    )
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="tool")()


def test_denied_admission_runs_after_hook_for_cleanup_with_same_call_identity():
    calls = []

    def lifecycle(context):
        calls.append((context.call_id, context.hook.phase, context.status))
        return Decision("deny" if context.hook.phase == "before" else "continue")

    run = run_with(lifecycle, hooks=[Hook("model"), Hook("model", "after")])
    with pytest.raises(HarnessDeniedError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    assert calls[0][0] == calls[1][0]
    assert [item[1:] for item in calls] == [("before", "pending"), ("after", "denied")]


def test_policy_state_is_atomic_within_a_run_and_isolated_across_runs():
    def first_only(context):
        count = context.state.get("count", 0)
        context.state["count"] = count + 1
        return Decision("continue" if count == 0 else "deny")

    harness = Harness(HarnessConfig("enforce", (policy(first_only),)))
    first, second = harness.start_run("first"), harness.start_run("second")
    barrier = Barrier(8)

    def worker(index):
        barrier.wait(timeout=30)
        try:
            return first.wrap(lambda: index, boundary="model", branch_id=str(index))()
        except HarnessDeniedError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(worker, range(8)))
    assert sum(output is not None for output in outputs) == 1
    assert {record.branch_id for record in first.results} == {str(index) for index in range(8)}
    assert second.wrap(lambda: "new run", boundary="model")() == "new run"


def test_async_work_executes_outside_policy_lock_and_cancellation_is_preserved():
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        run = run_with(lambda context: Decision())

        async def wait():
            started.set()
            await release.wait()

        task = asyncio.create_task(run.wrap(wait, boundary="model")())
        await started.wait()
        assert run.wrap(lambda: "concurrent", boundary="model")() == "concurrent"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert run.results[-1].status == "cancelled" and not run.stopped

    asyncio.run(exercise())


def test_generator_preserves_laziness_send_throw_return_and_close():
    run = run_with(lambda context: Decision())
    dispatched, closed = [], []
    failure = ValueError("throw")

    def source():
        dispatched.append(True)
        try:
            value = yield 1
            try:
                yield value
            except ValueError as exc:
                assert exc is failure
                yield 3
            return 99
        finally:
            closed.append(True)

    wrapped = run.wrap(source, boundary="model")
    assert inspect.isgeneratorfunction(wrapped)
    stream = wrapped()
    assert dispatched == [] and run.results == ()
    assert next(stream) == 1 and stream.send(2) == 2 and stream.throw(failure) == 3
    with pytest.raises(StopIteration) as completed:
        next(stream)
    assert completed.value.value == 99 and closed == [True]
    assert len(run.results) == 2 and run.results[-1].status == "ok"
    early = wrapped()
    next(early)
    early.close()
    assert run.results[-1].status == "closed"
    wrapped().close()
    assert len(run.results) == 4


def test_async_generator_preserves_laziness_send_throw_and_close():
    async def exercise():
        run = run_with(lambda context: Decision())
        closed = []
        failure = ValueError("throw")

        async def source():
            try:
                value = yield 1
                try:
                    yield value
                except ValueError as exc:
                    assert exc is failure
                    yield 3
            finally:
                closed.append(True)

        wrapped = run.wrap(source, boundary="model")
        assert inspect.isasyncgenfunction(wrapped)
        stream = wrapped()
        assert run.results == ()
        assert await stream.__anext__() == 1
        assert await stream.asend(2) == 2 and await stream.athrow(failure) == 3
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert run.results[-1].status == "ok"
        early = wrapped()
        await early.__anext__()
        await early.aclose()
        assert run.results[-1].status == "closed" and closed == [True, True]
        await wrapped().aclose()
        assert len(run.results) == 4

    asyncio.run(exercise())


def test_public_trace_context_and_existing_instrumentation_are_preserved():
    with trace_agent("outer") as trace:
        with trace_tool_call("parent"):
            parent_id = current_event_id()
            run = Harness(HarnessConfig("shadow")).start_run()

            @trace_tool()
            def nested(value):
                assert current_trace() is trace
                return value

            wrapped = run.wrap(nested, boundary="tool")
            assert inspect.signature(wrapped) == inspect.signature(nested)
            assert run.wrap(wrapped, boundary="tool") is wrapped
            assert wrapped(42) == 42
            assert current_trace() is trace and current_event_id() == parent_id
            assert trace.events[-1].parent_id == parent_id
            assert run.run_id == trace.run_id and run.results[0].trace_id == trace.run_id
            assert run.results[0].parent_span_id == parent_id
    assert current_trace() is None


def test_async_callable_objects_and_partials_keep_async_dispatch():
    class Worker:
        async def __call__(self, value):
            return value

    async def function(value):
        return value

    run = Harness(HarnessConfig("enforce")).start_run()
    assert asyncio.run(run.wrap(Worker(), boundary="tool")(7)) == 7
    assert asyncio.run(run.wrap(partial(function, 8), boundary="tool")()) == 8


def test_adapter_rejects_unsupported_hooks_actions_and_stream_lifecycles():
    with pytest.raises(ValueError, match="hook"):
        Harness(
            HarnessConfig("enforce", (policy(lambda context: Decision()),)),
            AdapterCapabilities("late", {Hook("model", "after")}, ACTIONS, EXECUTION_KINDS),
        )
    with pytest.raises(ValueError, match="action"):
        Harness(
            HarnessConfig("enforce", (policy(lambda context: Decision()),)),
            AdapterCapabilities("monitor", ALL_HOOKS, {"continue"}, EXECUTION_KINDS),
        )
    run = Harness(
        HarnessConfig("enforce"), AdapterCapabilities("sync", ALL_HOOKS, ACTIONS, {"sync"})
    ).start_run()

    def stream():
        yield 1

    with pytest.raises(ValueError, match="lifecycle"):
        run.wrap(stream, boundary="model")


def test_enforcement_requires_fail_closed_capabilities_even_for_continue_only_policy():
    monitor = AdapterCapabilities("monitor", ALL_HOOKS, {"continue"}, EXECUTION_KINDS)
    entry = policy(lambda context: Decision(), actions={"continue"})
    with pytest.raises(ValueError, match="fail-closed"):
        Harness(HarnessConfig("enforce", (entry,)), monitor)
    assert Harness().config.mode == "disabled"


def test_policy_reentrancy_is_observable_and_never_dispatches_nested_work():
    run = None

    def recursive(context):
        run.wrap(lambda: pytest.fail("nested dispatch"), boundary="model")()
        return Decision()

    run = run_with(recursive)
    with pytest.raises(HarnessEscalationError):
        run.wrap(lambda: pytest.fail("outer dispatch"), boundary="model")()
    assert run.results[0].proposals[0].failed


def test_after_hook_can_stop_future_admission_but_cannot_undo_completed_work():
    completed = []
    run = run_with(
        lambda context: Decision("stop"), hooks=[Hook("completion", "after")], actions={"stop"}
    )
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: completed.append(True), boundary="completion")()
    assert completed == [True] and run.results[-1].status == "ok"
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: completed.append(False), boundary="completion")()
    assert completed == [True]


def test_nested_wrappers_have_distinct_calls_without_losing_outer_context():
    run = Harness(HarnessConfig("shadow")).start_run()
    inner = run.wrap(lambda: "inner", boundary="tool", branch_id="child")
    outer = run.wrap(lambda: inner(), boundary="iteration")
    assert outer() == "inner"
    results = run.results
    assert [(item.hook.boundary, item.hook.phase) for item in results] == [
        ("iteration", "before"),
        ("tool", "before"),
        ("tool", "after"),
        ("iteration", "after"),
    ]
    assert results[0].call_id == results[-1].call_id
    assert results[1].call_id == results[2].call_id != results[0].call_id


def test_policy_cancellation_preserves_identity_and_blocks_enforced_dispatch():
    cancellation = asyncio.CancelledError("private cancellation")

    def cancelled(context):
        raise cancellation

    run = run_with(cancelled)
    with pytest.raises(asyncio.CancelledError) as error:
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model")()
    assert error.value is cancellation and run.stopped
    assert run.results[0].proposals[0].failed
    assert run.results[-1].status == "cancelled"
    assert "private cancellation" not in repr(run.results)


def test_invalid_post_dispatch_denial_escalates_and_keeps_work_outcome():
    def invalid(context):
        return Decision("continue" if context.hook.phase == "before" else "deny")

    run = run_with(invalid, hooks=[Hook("model"), Hook("model", "after")])
    called = []
    with pytest.raises(HarnessEscalationError):
        run.wrap(lambda: called.append(True), boundary="model")()
    assert called == [True]
    assert run.results[-1].status == "ok" and run.results[-1].proposals[0].failed


@pytest.mark.parametrize("kind", ["generator", "async_generator"])
def test_stream_failure_is_preserved_when_after_policy_also_fails(kind):
    failure = ValueError("original stream error")

    def generator():
        yield 1
        raise failure

    async def async_generator():
        yield 1
        raise failure

    def after(context):
        raise RuntimeError("policy failed")

    run = run_with(after, hooks=[Hook("model", "after")], actions={"continue"})
    wrapped = run.wrap(generator if kind == "generator" else async_generator, boundary="model")
    if kind == "generator":
        stream = wrapped()
        assert next(stream) == 1
        with pytest.raises(ValueError) as caught:
            next(stream)
        assert caught.value is failure
    else:

        async def consume():
            stream = wrapped()
            assert await stream.__anext__() == 1
            with pytest.raises(ValueError) as caught:
                await stream.__anext__()
            assert caught.value is failure

        asyncio.run(consume())
    assert run.stopped and run.results[-1].status == "error"


def test_duplicate_ids_versions_modes_and_async_policies_are_rejected():
    entry = policy(lambda context: Decision())
    with pytest.raises(ValueError, match="unique"):
        HarnessConfig("enforce", (entry, entry))
    with pytest.raises(ValueError, match="mode"):
        HarnessConfig("automatic")
    with pytest.raises(ValueError, match="version"):
        HarnessConfig(schema_version="2.0")

    async def unsupported(context):
        return Decision()

    with pytest.raises(ValueError, match="synchronous"):
        policy(unsupported)


@pytest.mark.parametrize("cancel_policy", [False, True])
def test_cleanup_distinguishes_cancellation_before_and_after_dispatch(cancel_policy):
    cancellation = asyncio.CancelledError()
    observed = []
    invoked = []

    def lifecycle(context):
        if context.hook.phase == "before" and cancel_policy:
            raise cancellation
        if context.hook.phase == "after":
            observed.append((context.status, context.dispatched))
        return Decision()

    def target():
        invoked.append(True)
        raise cancellation

    run = run_with(lifecycle, hooks=[Hook("model"), Hook("model", "after")])
    with pytest.raises(asyncio.CancelledError) as caught:
        run.wrap(target, boundary="model")()
    assert caught.value is cancellation
    assert observed == [("cancelled", not cancel_policy)]
    assert invoked == ([] if cancel_policy else [True])
    assert run.results[0].dispatched is False
    assert run.results[-1].dispatched is (not cancel_policy)
