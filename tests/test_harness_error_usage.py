from __future__ import annotations

import asyncio

import pytest

from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig, HarnessDeniedError


def configured():
    return Harness(
        HarnessConfig(mode="enforce", policies=(budget_policy(BudgetLimits(max_tokens=5)),))
    ).start_run()


def options(tokens):
    return DispatchOptions(Reservation(tokens=tokens, provenance="upper_bound"))


@pytest.mark.parametrize("kind", ["sync", "async", "generator", "async_generator"])
def test_sdk_error_usage_is_charged_across_all_lifecycles(kind):
    error = ValueError("PRIVATE SDK response")

    def sync():
        raise error

    async def asynchronous():
        raise error

    def stream():
        yield "chunk"
        raise error

    async def astream():
        yield "chunk"
        raise error

    run = configured()
    seen = []

    def inspect_error(value):
        seen.append(value)
        return ResourceUsage(tokens=4, token_provenance="provider", complete=True)

    call = run.wrap(
        {"sync": sync, "async": asynchronous, "generator": stream, "async_generator": astream}[
            kind
        ],
        boundary="model",
        dispatch=options(5),
        error_usage_reader=inspect_error,
    )
    with pytest.raises(ValueError) as caught:
        if kind == "sync":
            call()
        elif kind == "async":
            asyncio.run(call())
        elif kind == "generator":
            list(call())
        else:

            async def consume():
                return [item async for item in call()]

            asyncio.run(consume())
    assert caught.value is error and seen == [error]
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("failed call's tokens were not charged"),
            boundary="model",
            dispatch=options(2),
        )()
    assert "PRIVATE" not in str(run.export_evidence())


@pytest.mark.parametrize("reader_failure", [ValueError("PRIVATE collector"), KeyboardInterrupt()])
def test_bad_error_reader_cannot_replace_provider_failure(reader_failure):
    original = RuntimeError("PRIVATE original")

    def provider():
        raise original

    def reader(value):
        raise reader_failure

    run = configured()
    call = run.wrap(provider, boundary="model", dispatch=options(5), error_usage_reader=reader)
    with pytest.raises(RuntimeError) as caught:
        call()
    assert caught.value is original and "PRIVATE" not in str(run.export_evidence())
    snapshots = [
        proposal.decision.budget_snapshot
        for hook in run.results
        for proposal in hook.proposals
        if proposal.decision.budget_snapshot is not None
    ]
    expected = "collector_error" if isinstance(reader_failure, Exception) else "collector_cancelled"
    assert snapshots[-1].to_dict()["usage_error"] == expected
    assert not any(proposal.failed for hook in run.results for proposal in hook.proposals)


def test_error_reader_never_runs_for_denied_or_successful_work_and_is_idempotent():
    run = configured()

    def reader(value):
        pytest.fail("no SDK failure occurred")

    call = run.wrap(
        lambda: 7,
        boundary="model",
        dispatch=options(1),
        usage_reader=lambda value: ResourceUsage(
            tokens=1, token_provenance="provider", complete=True
        ),
        error_usage_reader=reader,
    )
    assert call() == 7
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("not admitted"),
            boundary="model",
            dispatch=options(5),
            error_usage_reader=reader,
        )()
    plain = Harness(HarnessConfig(mode="shadow")).start_run()

    def target():
        return 1

    wrapped = plain.wrap(target, boundary="model", error_usage_reader=reader)
    assert plain.wrap(wrapped, boundary="model", error_usage_reader=reader) is wrapped
    with pytest.raises(ValueError, match="original callable"):
        plain.wrap(wrapped, boundary="model", error_usage_reader=lambda error: None)
