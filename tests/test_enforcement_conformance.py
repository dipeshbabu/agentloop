from __future__ import annotations

import asyncio
from functools import wraps

import pytest
from enforcement_conformance import EnforcementContract, ProtocolGraph, ProtocolSignalError

from agentloop import current_trace, trace_agent
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import ACTIONS, Decision, HarnessConfig, HarnessDeniedError, Hook, Policy
from agentloop.integrations import langgraph_harness as integration
from agentloop.tracer import current_event_id, trace_tool_call


@pytest.fixture(autouse=True)
def protocol_runtime(monkeypatch):
    monkeypatch.setattr(integration, "_check_version", lambda: None)
    monkeypatch.setattr(integration, "_signal_type", lambda: ProtocolSignalError)
    monkeypatch.setattr(integration, "_runtime_identity", lambda: ("task-1", 1))


@pytest.fixture(params=["python", "langgraph"])
def contract(request):
    return EnforcementContract(request.param)


def config(mode="enforce", action="deny", boundary="model"):
    return HarnessConfig(
        mode,
        (
            Policy(
                "guard",
                "1",
                lambda ctx: Decision(action),
                hooks=frozenset({Hook(boundary)}),
                actions=ACTIONS,
            ),
        ),
    )


def functions(calls, output=7):
    def sync(value):
        calls.append(value)
        return output

    async def asynchronous(value):
        return sync(value)

    def stream(value):
        yield sync(value)

    async def astream(value):
        yield sync(value)

    return {"sync": sync, "async": asynchronous, "generator": stream, "async_generator": astream}


def execute(fn, kind, value=3):
    if kind == "sync":
        return fn(value)
    if kind == "async":
        return asyncio.run(fn(value))
    if kind == "generator":
        return list(fn(value))

    async def consume():
        return [item async for item in fn(value)]

    return asyncio.run(consume())


@pytest.mark.parametrize("kind", ["sync", "async", "generator", "async_generator"])
@pytest.mark.parametrize("boundary", ["model", "tool"])
def test_denial_prevents_dispatch(contract, kind, boundary):
    calls = []
    fn, _ = contract.bind(
        config(boundary=boundary), functions(calls)[kind], kind, boundary=boundary
    )
    with pytest.raises(HarnessDeniedError):
        execute(fn, kind)
    assert calls == []


@pytest.mark.parametrize("kind", ["sync", "async", "generator", "async_generator"])
@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_disabled_shadow_preserve_results(contract, kind, mode):
    calls = []
    fn, _ = contract.bind(config(mode), functions(calls)[kind], kind)
    assert execute(fn, kind) == ([7] if "generator" in kind else 7)
    assert calls == [3]


def test_arguments_result_identity_and_usage_reader(contract):
    from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage

    calls, reads = [], []
    response = {"private": "response", "tokens": 2}

    def work(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    def usage(value):
        reads.append(value)
        return ResourceUsage(
            tokens=value["tokens"], token_provenance="provider_reported", complete=True
        )

    fn, _ = contract.bind(
        config(action="continue"),
        work,
        "sync",
        dispatch=DispatchOptions(reservation=Reservation(tokens=3, provenance="upper_bound")),
        usage_reader=usage,
    )
    with trace_agent("private") as trace:
        assert fn("private-argument", key="private-key") is response
    assert calls == [(("private-argument",), {"key": "private-key"})]
    assert reads == [response]
    assert "private-argument" not in str(trace.metadata)
    assert "private-key" not in str(trace.metadata)
    assert "response" not in str(trace.metadata)


@pytest.mark.parametrize("kind", ["sync", "async", "generator", "async_generator"])
@pytest.mark.parametrize("error", [ValueError("original"), asyncio.CancelledError("original")])
def test_original_failure_identity(contract, kind, error):
    def fail(value):
        raise error

    async def afail(value):
        raise error

    def stream(value):
        raise error
        yield

    async def astream(value):
        raise error
        yield

    fn, _ = contract.bind(
        config(action="continue"),
        {"sync": fail, "async": afail, "generator": stream, "async_generator": astream}[kind],
        kind,
    )
    with pytest.raises(type(error)) as caught:
        execute(fn, kind)
    assert caught.value is error


def test_sync_stream_protocol_laziness_and_context(contract):
    calls = []
    parent = None
    with trace_agent("stream") as trace:
        with trace_tool_call("parent"):
            parent = current_event_id()

            def stream(value):
                calls.append((current_trace(), current_event_id()))
                try:
                    sent = yield value
                    try:
                        yield sent
                    except ValueError as exc:
                        yield exc
                finally:
                    calls.append("closed")

            fn, get_run = contract.bind(config(action="continue"), stream, "generator")
            source = fn(3)
            assert not calls
            assert next(source) == 3
            assert source.send(9) == 9
            error = ValueError("throw")
            assert source.throw(error) is error
            source.close()
            assert current_trace() is trace and current_event_id() == parent
            assert not any(r.hook.boundary == "completion" for r in get_run().results)
    assert calls == [(trace, parent), "closed"]
    assert current_trace() is None and current_event_id() is None


def test_async_stream_protocol_and_caller_context(contract):
    async def scenario():
        seen = []

        async def source(value):
            try:
                sent = yield value
                try:
                    yield sent
                except ValueError as exc:
                    yield exc
            finally:
                seen.append("closed")

        fn, get_run = contract.bind(config(action="continue"), source, "async_generator")
        with trace_agent("async") as trace:
            stream = fn(3)
            assert await stream.__anext__() == 3
            assert current_trace() is trace
            assert await stream.asend(9) == 9
            error = ValueError("throw")
            assert await stream.athrow(error) is error
            await stream.aclose()
            assert current_trace() is trace
            assert not any(r.hook.boundary == "completion" for r in get_run().results)
        assert seen == ["closed"]

    asyncio.run(scenario())


def test_adapter_repeated_instrumentation_and_copied_metadata():
    adapter = integration.LangGraphHarness(config(action="continue"), boundaries={"model"})
    calls = []
    original = functions(calls)["sync"]
    wrapped = adapter.model(original)
    assert adapter.model(wrapped) is wrapped

    @wraps(wrapped)
    def copy(value):
        return original(value)

    assert adapter.model(copy) is not copy
    graph = adapter.runnable(ProtocolGraph(wrapped))
    assert adapter.runnable(graph) is graph
    assert graph.invoke(3) == 7
    assert len(adapter.last_run.results) == 2
    with pytest.raises(RuntimeError, match="runnable"):
        wrapped(4)


def test_capability_and_generator_node_rejection():
    with pytest.raises(ValueError, match="policy hook"):
        integration.LangGraphHarness(config())
    adapter = integration.LangGraphHarness(HarnessConfig("enforce"))
    with pytest.raises(ValueError, match="declared"):
        adapter.model(lambda: None)
    with pytest.raises(ValueError, match="generator nodes"):
        adapter.node("node")(functions([])["generator"])


def test_parallel_runs_do_not_share_budgets_or_last_run():
    async def scenario():
        adapter = integration.LangGraphHarness(
            HarnessConfig(
                "enforce", (budget_policy(BudgetLimits(max_model_calls=1), boundaries={"model"}),)
            ),
            boundaries={"model"},
        )

        async def work(value):
            await asyncio.sleep(0)
            return value

        app = adapter.runnable(ProtocolGraph(adapter.model(work)))

        async def run(value):
            result = await app.ainvoke(value)
            return result, adapter.last_run

        first, second = await asyncio.gather(run(1), run(2))
        assert (first[0], second[0]) == (1, 2)
        assert first[1] is not second[1]
        assert adapter.last_run is None

    asyncio.run(scenario())


def test_stream_session_does_not_leak_between_resumes():
    adapter = integration.LangGraphHarness(config(action="continue"), boundaries={"model"})
    wrapped = adapter.model(functions([])["generator"])
    source = adapter.runnable(ProtocolGraph(wrapped)).stream(1)
    assert next(source) == 7
    with pytest.raises(RuntimeError, match="runnable"):
        next(wrapped(2))
    source.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_untraced_stream_does_not_capture_a_later_callers_trace(asynchronous):
    observed = []
    adapter = integration.LangGraphHarness(config(action="continue"), boundaries={"model"})

    def chunks(value):
        observed.append(current_trace())
        yield value
        observed.append(current_trace())
        yield value + 1

    async def achunks(value):
        for chunk in chunks(value):
            yield chunk

    async def scenario():
        app = adapter.runnable(ProtocolGraph(adapter.model(achunks)))
        stream = app.astream(1)
        assert await stream.__anext__() == 1
        with trace_agent("unrelated") as trace:
            assert await stream.__anext__() == 2
            await stream.aclose()
            assert current_trace() is trace
            assert not trace.metadata.get("agentloop.harness")

    if asynchronous:
        asyncio.run(scenario())
    else:
        app = adapter.runnable(ProtocolGraph(adapter.model(chunks)))
        stream = app.stream(1)
        assert next(stream) == 1
        with trace_agent("unrelated") as trace:
            assert next(stream) == 2
            stream.close()
            assert current_trace() is trace
            assert not trace.metadata.get("agentloop.harness")
    assert observed == [None, None]


def test_disabled_needs_no_sdk_and_returns_originals(monkeypatch):
    monkeypatch.setattr(integration, "_check_version", lambda: pytest.fail("SDK accessed"))
    adapter = integration.LangGraphHarness()

    def fn():
        return None

    app = object()
    assert adapter.node("node")(fn) is fn
    assert adapter.model(fn) is fn
    assert adapter.tool(fn) is fn
    assert adapter.runnable(app) is app


def test_version_guard(monkeypatch):
    # The real guard is tested separately from protocol-boundary tests.
    monkeypatch.undo()
    monkeypatch.setattr(integration, "version", lambda name: "0.0.0")
    with pytest.raises(RuntimeError, match="1.2.11"):
        integration.LangGraphHarness(HarnessConfig("shadow")).runnable(ProtocolGraph(lambda: 1))
