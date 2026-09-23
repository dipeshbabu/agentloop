from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import (
    Decision,
    Harness,
    HarnessConfig,
    HarnessDeniedError,
    HarnessEscalationError,
    HarnessStoppedError,
    Hook,
    Policy,
)


def make_run(limits, *, mode="enforce", **options):
    return Harness(HarnessConfig(mode, (budget_policy(limits, **options),))).start_run()


def last_snapshot(run):
    for result in reversed(run.results):
        for proposal in result.proposals:
            if proposal.decision.budget_snapshot is not None:
                return proposal.decision.budget_snapshot.to_dict()
    pytest.fail("budget snapshot missing")


def bounds(tokens=None, cost=None, **kwargs):
    return DispatchOptions(Reservation(tokens, cost, "upper_bound", True, **kwargs))


def usage(tokens=None, cost=None, **kwargs):
    return ResourceUsage(tokens, cost, "provider", "provider_reported", complete=True, **kwargs)


@pytest.mark.parametrize(
    "boundary,field",
    [("model", "max_model_calls"), ("tool", "max_tool_calls"), ("iteration", "max_iterations")],
)
def test_call_capacity_denies_before_dispatch_and_retains_committed_count(boundary, field):
    run = make_run(BudgetLimits(**{field: 1}))
    calls = []
    target = run.wrap(lambda: calls.append(True), boundary=boundary)
    target()
    with pytest.raises(HarnessDeniedError):
        target()
    snapshot = last_snapshot(run)
    assert calls == [True]
    assert snapshot["committed"][field.removeprefix("max_")] == 1
    assert snapshot["in_flight"][field.removeprefix("max_")] == 0
    assert snapshot["hard_spend_cap"] is False


def test_concurrent_tasks_cannot_all_consume_the_last_call_slot():
    run = make_run(BudgetLimits(max_model_calls=1))
    start, entered, release = Barrier(8), Event(), Event()
    calls = []

    def work():
        calls.append(True)
        entered.set()
        assert release.wait(timeout=30)
        return "done"

    def attempt(index):
        start.wait(timeout=30)
        try:
            return run.wrap(work, boundary="model", branch_id=str(index))()
        except HarnessDeniedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt, index) for index in range(8)]
        assert entered.wait(timeout=30)
        assert last_snapshot(run)["in_flight"]["model_calls"] == 1
        release.set()
        results = [future.result(timeout=30) for future in futures]
    assert results.count("done") == 1 and results.count("denied") == 7
    assert len(calls) == 1 and last_snapshot(run)["committed"]["model_calls"] == 1


def test_budget_policy_is_reusable_without_sharing_state_between_runs():
    harness = Harness(HarnessConfig("enforce", (budget_policy(BudgetLimits(max_tool_calls=1)),)))
    first, second = harness.start_run("first"), harness.start_run("second")
    for run in (first, second):
        wrapped = run.wrap(lambda: "ok", boundary="tool")
        assert wrapped() == "ok"
        with pytest.raises(HarnessDeniedError):
            wrapped()
        assert last_snapshot(run)["committed"]["tool_calls"] == 1


def test_other_policy_denial_refunds_reserved_capacity_without_unknown_charges():
    budget = budget_policy(BudgetLimits(max_model_calls=1, max_tokens=20, max_cost_usd=1))
    denial = Policy(
        "review",
        "1",
        lambda context: Decision("deny"),
        hooks={Hook("model")},
        actions={"deny"},
        priority=200,
    )
    run = Harness(HarnessConfig("enforce", (budget, denial))).start_run()
    with pytest.raises(HarnessDeniedError):
        run.wrap(lambda: pytest.fail("dispatched"), boundary="model", dispatch=bounds(20, 1))()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 0
    assert snapshot["in_flight"]["model_calls"] == 0
    assert snapshot["refunded"]["model_calls"] == 1
    assert snapshot["refunded"]["tokens_bound"] == 20
    assert snapshot["refunded"]["cost_bound_usd"] == "1"
    assert snapshot["unknown_usage"]["calls"] == 0


def test_shadow_denials_still_account_for_work_that_actually_runs():
    run = make_run(BudgetLimits(max_model_calls=0), mode="shadow")
    output = object()
    target = run.wrap(lambda: output, boundary="model")
    assert target() is output and target() is output
    assert last_snapshot(run)["committed"]["model_calls"] == 2
    assert last_snapshot(run)["in_flight"]["model_calls"] == 0
    assert all(not result.applied for result in run.results)


def test_retry_limits_preserve_explicit_origins_and_check_each_attempt():
    run = make_run(BudgetLimits(max_retries=2))
    calls = []

    def function():
        calls.append(True)

    run.wrap(function, boundary="model")()
    for source in ("framework", "provider"):
        run.wrap(function, boundary="model", dispatch=DispatchOptions(retry_source=source))()
    with pytest.raises(HarnessDeniedError):
        run.wrap(function, boundary="model", dispatch=DispatchOptions(retry_source="harness"))()
    snapshot = last_snapshot(run)
    assert len(calls) == 3 and snapshot["committed"]["retries"] == 2
    assert snapshot["retry_sources"] == {"framework": 1, "provider": 1, "harness": 0}


def test_nested_attempts_see_outer_reservations():
    run = make_run(BudgetLimits(max_model_calls=1))
    child = run.wrap(lambda: pytest.fail("nested model dispatched"), boundary="model")
    parent = run.wrap(child, boundary="iteration")
    outer = run.wrap(parent, boundary="model", branch_id="outer")
    with pytest.raises(HarnessDeniedError):
        outer()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 1
    assert snapshot["committed"]["iterations"] == 1
    assert snapshot["in_flight"]["model_calls"] == 0


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_deadline_checks_are_monotonic_and_do_not_kill_already_admitted_work():
    clock = Clock()
    run = make_run(BudgetLimits(deadline_at=5), clock=clock)
    output = object()

    def work():
        clock.now = 10
        return output

    assert run.wrap(work, boundary="tool")() is output
    assert last_snapshot(run)["deadline_remaining_s"] == 0
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: pytest.fail("late work dispatched"), boundary="tool")()


def test_clock_regression_fails_closed_but_reconciles_completed_work():
    clock = Clock()
    clock.now = 10
    run = make_run(BudgetLimits(deadline_at=20), clock=clock)

    def work():
        clock.now = 5
        return "finished"

    with pytest.raises(HarnessEscalationError):
        run.wrap(work, boundary="tool")()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["tool_calls"] == 1
    assert snapshot["in_flight"]["tool_calls"] == 0
    assert "clock_invalid" in snapshot["status_codes"]
    assert snapshot["deadline_remaining_s"] is None


def test_soft_threshold_is_observable_without_denying_valid_admission():
    run = make_run(BudgetLimits(max_model_calls=2), soft_fraction=0.5)
    assert run.wrap(lambda: "ok", boundary="model")() == "ok"
    before = run.results[0].proposals[0].decision
    assert before.action == "continue" and before.reason_code == "budget_soft_limit"
    assert "model_calls" in before.budget_snapshot.to_dict()["soft_exceeded"]


def test_known_usage_reconciles_reservations_and_releases_unused_capacity():
    run = make_run(BudgetLimits(max_tokens=20, max_cost_usd=1))

    def target():
        return "value"

    first = run.wrap(
        target, boundary="model", dispatch=bounds(10, 0.1), usage_reader=lambda _: usage(6, 0.06)
    )
    assert first() == "value"
    second = run.wrap(
        target, boundary="model", dispatch=bounds(14, 0.94), usage_reader=lambda _: usage(14, 0.14)
    )
    assert second() == "value"
    snapshot = last_snapshot(run)
    assert snapshot["total_usage"] == {"tokens": 20, "cost_usd": "0.2"}
    assert snapshot["refunded"]["tokens_bound"] == 4
    assert snapshot["refunded"]["cost_bound_usd"] == "0.84"
    assert snapshot["in_flight"]["metered_calls"] == 0
    with pytest.raises(HarnessDeniedError):
        run.wrap(target, boundary="model", dispatch=bounds(1, 0.1))()


def test_decimal_cost_boundary_has_no_cumulative_float_drift():
    run = make_run(BudgetLimits(max_cost_usd=1))
    calls = []

    def target():
        calls.append(True)

    wrapped = run.wrap(
        target, boundary="model", dispatch=bounds(cost=0.1), usage_reader=lambda _: usage(1, 0.1)
    )
    for _ in range(10):
        wrapped()
    assert last_snapshot(run)["total_usage"]["cost_usd"] == "1"
    with pytest.raises(HarnessDeniedError):
        wrapped()
    assert len(calls) == 10


@pytest.mark.parametrize(
    "unknown_policy,error",
    [("deny", HarnessDeniedError), ("escalate", HarnessEscalationError), ("monitor_only", None)],
)
def test_missing_bounds_follow_the_explicit_unknown_policy(unknown_policy, error):
    run = make_run(BudgetLimits(max_tokens=10), unknown_usage=unknown_policy)
    calls = []
    wrapped = run.wrap(lambda: calls.append(True), boundary="model")
    if error is not None:
        with pytest.raises(error):
            wrapped()
        assert calls == []
    else:
        wrapped()
        assert calls == [True]
        assert last_snapshot(run)["total_usage"]["tokens"] is None
        assert last_snapshot(run)["unknown_usage"]["tokens"] == 1


@pytest.mark.parametrize(
    "reservation,limits",
    [
        (Reservation(10, 0.1, "estimated", True), BudgetLimits(max_tokens=20)),
        (Reservation(10, 0.1, "upper_bound", False), BudgetLimits(max_cost_usd=1)),
    ],
)
def test_estimated_bounds_and_unknown_pricing_never_authorize_enforced_spend(reservation, limits):
    run = make_run(limits)
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("dispatched"),
            boundary="model",
            dispatch=DispatchOptions(reservation),
        )()


@pytest.mark.parametrize(
    "provenance", [None, "legacy", "unspecified", "estimated_words", "unavailable"]
)
def test_legacy_or_estimated_usage_remains_unknown_after_dispatch(provenance):
    run = make_run(BudgetLimits(max_tokens=20))

    def reader(_):
        return ResourceUsage(tokens=5, token_provenance=provenance, complete=True)

    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: "value", boundary="model", dispatch=bounds(20), usage_reader=reader)()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 1
    assert snapshot["committed"]["tokens_known"] == 0
    assert snapshot["total_usage"]["tokens"] is None
    assert snapshot["unknown_usage"]["tokens_held"] == 20
    assert snapshot["refunded"]["tokens_bound"] == 0


@pytest.mark.parametrize(
    "failure", [ValueError("private failure"), asyncio.CancelledError("private cancellation")]
)
def test_failed_or_cancelled_calls_remain_potentially_billable_and_keep_exception_identity(failure):
    run = make_run(BudgetLimits(max_model_calls=2, max_tokens=20, max_cost_usd=1))

    def target():
        raise failure

    with pytest.raises(type(failure)) as caught:
        run.wrap(target, boundary="model", dispatch=bounds(10, 0.5))()
    assert caught.value is failure and run.stopped
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 1
    assert snapshot["in_flight"]["model_calls"] == 0
    assert snapshot["unknown_usage"]["calls"] == 1
    assert snapshot["unknown_usage"]["cost_held_usd"] == "0.5"
    assert snapshot["total_usage"] == {"tokens": None, "cost_usd": None}
    assert snapshot["refunded"]["cost_bound_usd"] == "0"


def test_realized_usage_above_declared_bound_reports_overshoot_and_escalates():
    run = make_run(BudgetLimits(max_tokens=10, max_cost_usd=0.1))
    calls = []
    with pytest.raises(HarnessEscalationError):
        run.wrap(
            lambda: calls.append(True),
            boundary="model",
            dispatch=bounds(10, 0.1),
            usage_reader=lambda _: usage(12, 0.15),
        )()
    snapshot = last_snapshot(run)
    assert calls == [True] and run.stopped
    assert snapshot["overshoot"] == {"tokens": 2, "cost_usd": "0.05"}
    assert "bound_exceeded" in snapshot["status_codes"]
    assert snapshot["hard_spend_cap"] is False
    assert snapshot["spend_enforcement"] == "best_effort"
    assert snapshot["possible_spend_overshoot"] is True


def test_duplicate_complete_usage_is_charged_once_but_attempts_are_counted():
    run = make_run(BudgetLimits(max_tokens=20, max_cost_usd=1))
    report = usage(5, 0.05, usage_id="provider/request-1")
    wrapped = run.wrap(
        lambda: "cached", boundary="model", dispatch=bounds(10, 0.1), usage_reader=lambda _: report
    )
    wrapped()
    wrapped()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 2
    assert snapshot["total_usage"] == {"tokens": 5, "cost_usd": "0.05"}
    assert "duplicate_usage" in snapshot["status_codes"]


def test_conflicting_usage_identity_does_not_overwrite_previous_measurements():
    run = make_run(BudgetLimits(max_tokens=20), unknown_usage="monitor_only")
    reports = iter([usage(5, 0.05, usage_id="same"), usage(6, 0.06, usage_id="same")])
    wrapped = run.wrap(
        lambda: "value",
        boundary="model",
        dispatch=bounds(10, 0.1),
        usage_reader=lambda _: next(reports),
    )
    wrapped()
    wrapped()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["tokens_known"] == 5
    assert snapshot["total_usage"]["tokens"] is None
    assert "usage_conflict" in snapshot["status_codes"]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_partial_stream_close_preserves_unknown_usage_and_held_reservations(kind):
    run = make_run(BudgetLimits(max_tokens=20, max_cost_usd=1))
    partial = ResourceUsage(3, 0.03, "provider", "provider_reported", complete=False)

    def stream():
        yield partial
        yield usage(5, 0.05)

    async def astream():
        yield partial
        yield usage(5, 0.05)

    if kind == "sync":
        result = run.wrap(
            stream, boundary="model", dispatch=bounds(10, 0.1), usage_reader=lambda value: value
        )()
        assert next(result) is partial
        result.close()
    else:

        async def exercise():
            result = run.wrap(
                astream,
                boundary="model",
                dispatch=bounds(10, 0.1),
                usage_reader=lambda value: value,
            )()
            assert await result.__anext__() is partial
            await result.aclose()

        asyncio.run(exercise())
    snapshot = last_snapshot(run)
    assert snapshot["unknown_usage"]["calls"] == 1
    assert snapshot["unknown_usage"]["tokens_held"] == 10
    assert snapshot["unknown_usage"]["cost_held_usd"] == "0.1"
    assert snapshot["total_usage"] == {"tokens": None, "cost_usd": None}
    assert snapshot["in_flight"]["model_calls"] == 0 and run.stopped


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_cumulative_stream_usage_is_replaced_not_summed(kind):
    run = make_run(BudgetLimits(max_tokens=10, max_cost_usd=0.1))
    reports = [ResourceUsage(3, 0.03, "provider", "provider_reported"), usage(5, 0.05)]

    def stream():
        yield from reports

    async def astream():
        for report in reports:
            yield report

    if kind == "sync":
        assert (
            list(
                run.wrap(
                    stream,
                    boundary="model",
                    dispatch=bounds(10, 0.1),
                    usage_reader=lambda value: value,
                )()
            )
            == reports
        )
    else:

        async def exercise():
            return [
                item
                async for item in run.wrap(
                    astream,
                    boundary="model",
                    dispatch=bounds(10, 0.1),
                    usage_reader=lambda value: value,
                )()
            ]

        assert asyncio.run(exercise()) == reports
    snapshot = last_snapshot(run)
    assert snapshot["total_usage"] == {"tokens": 5, "cost_usd": "0.05"}
    assert snapshot["refunded"]["tokens_bound"] == 5
    assert snapshot["refunded"]["cost_bound_usd"] == "0.05"


def test_observed_generator_preserves_send_throw_return_and_lazy_admission():
    run = make_run(BudgetLimits(max_tokens=10))
    failure = ValueError("original")

    def stream():
        received = yield 1
        try:
            yield received
        except ValueError as caught:
            assert caught is failure
            yield 3
        return 99

    def read(value):
        return usage(5, 0.05) if value == 99 else None

    result = run.wrap(stream, boundary="model", dispatch=bounds(10), usage_reader=read)()
    assert run.results == ()
    assert next(result) == 1 and result.send(2) == 2 and result.throw(failure) == 3
    with pytest.raises(StopIteration) as ended:
        next(result)
    assert ended.value.value == 99
    assert last_snapshot(run)["total_usage"]["tokens"] == 5


def test_async_cancellation_retains_reservations_and_original_cancellation():
    async def exercise():
        run = make_run(BudgetLimits(max_tokens=10))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model():
            entered.set()
            await release.wait()

        task = asyncio.create_task(run.wrap(model, boundary="model", dispatch=bounds(10))())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert last_snapshot(run)["unknown_usage"]["tokens_held"] == 10
        assert last_snapshot(run)["total_usage"]["tokens"] is None
        assert run.stopped

    asyncio.run(exercise())


def test_collector_failures_are_safe_unknowns_and_do_not_change_shadow_results():
    import json

    secret = "private response and exception"
    run = make_run(BudgetLimits(max_tokens=10), mode="shadow")
    output = {"text": secret}

    def broken(value):
        assert value is output
        raise ValueError(secret)

    wrapped = run.wrap(lambda: output, boundary="model", dispatch=bounds(10), usage_reader=broken)
    assert wrapped() is output
    snapshot = last_snapshot(run)
    assert snapshot["usage_error"] == "collector_error"
    assert snapshot["total_usage"]["tokens"] is None
    assert secret not in json.dumps(run.export_evidence())


def test_disabled_budget_keeps_original_callable_and_does_not_collect_usage():
    run = make_run(BudgetLimits(max_model_calls=0), mode="disabled")

    def target():
        return "unchanged"

    wrapped = run.wrap(
        target, boundary="model", usage_reader=lambda _: pytest.fail("collector ran")
    )
    assert wrapped is target and wrapped() == "unchanged"
    assert run.results == ()


def test_rewrapping_with_different_dispatch_settings_is_explicitly_rejected():
    run = make_run(BudgetLimits(max_model_calls=1))
    wrapped = run.wrap(lambda: None, boundary="model", dispatch=bounds(10))
    assert run.wrap(wrapped, boundary="model", dispatch=bounds(10)) is wrapped
    with pytest.raises(ValueError, match="original callable"):
        run.wrap(wrapped, boundary="model", dispatch=bounds(20))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_calls", -1),
        ("max_tool_calls", True),
        ("max_iterations", 1.5),
        ("max_retries", float("inf")),
        ("max_tokens", -1),
        ("max_cost_usd", float("nan")),
        ("max_cost_usd", -0.1),
        ("deadline_at", float("inf")),
    ],
)
def test_invalid_limits_are_rejected_before_execution(field, value):
    with pytest.raises(ValueError):
        BudgetLimits(**{field: value})


def test_invalid_scope_and_soft_thresholds_are_rejected():
    with pytest.raises(ValueError, match="boundary"):
        budget_policy(BudgetLimits(max_model_calls=1), boundaries={"tool"})
    with pytest.raises(ValueError, match="metered"):
        budget_policy(BudgetLimits(max_tokens=1), metered_boundaries=())
    with pytest.raises(ValueError, match="soft_fraction"):
        budget_policy(BudgetLimits(), soft_fraction=1.1)


def test_async_concurrency_reserves_token_and_cost_capacity_before_awaiting_work():
    async def exercise():
        run = make_run(BudgetLimits(max_tokens=10, max_cost_usd=0.1))
        entered, release = asyncio.Event(), asyncio.Event()

        async def model():
            entered.set()
            await release.wait()
            return "finished"

        wrapped = run.wrap(
            model, boundary="model", dispatch=bounds(10, 0.1), usage_reader=lambda _: usage(5, 0.05)
        )
        first = asyncio.create_task(wrapped())
        await entered.wait()
        snapshot = last_snapshot(run)
        assert snapshot["in_flight"]["tokens_reserved"] == 10
        assert snapshot["in_flight"]["cost_reserved_usd"] == "0.1"
        with pytest.raises(HarnessDeniedError):
            await wrapped()
        release.set()
        assert await first == "finished"
        assert last_snapshot(run)["total_usage"] == {"tokens": 5, "cost_usd": "0.05"}

    asyncio.run(exercise())


def test_token_and_cost_budgets_accept_explicit_complete_zero_usage():
    run = make_run(BudgetLimits(max_tokens=0, max_cost_usd=0))
    assert (
        run.wrap(
            lambda: "free",
            boundary="model",
            dispatch=bounds(0, 0),
            usage_reader=lambda _: usage(0, 0),
        )()
        == "free"
    )
    assert last_snapshot(run)["total_usage"] == {"tokens": 0, "cost_usd": "0"}
    assert last_snapshot(run)["unknown_usage"]["calls"] == 0


@pytest.mark.parametrize(
    "token_provenance,pricing_known", [("estimated_words", True), ("provider", False)]
)
def test_calculated_cost_requires_known_pricing_and_an_exact_token_basis(
    token_provenance, pricing_known
):
    run = make_run(BudgetLimits(max_cost_usd=1))

    def reader(_):
        return ResourceUsage(
            5, 0.05, token_provenance, "calculated", pricing_known=pricing_known, complete=True
        )

    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: None, boundary="model", dispatch=bounds(cost=0.1), usage_reader=reader)()
    assert last_snapshot(run)["total_usage"]["cost_usd"] is None


def test_provider_reported_cost_does_not_require_a_token_estimate_or_price_catalog():
    run = make_run(BudgetLimits(max_cost_usd=1))

    def reader(_):
        return ResourceUsage(cost_usd=0.05, cost_provenance="provider_reported", complete=True)

    run.wrap(lambda: None, boundary="model", dispatch=bounds(cost=0.1), usage_reader=reader)()
    assert last_snapshot(run)["total_usage"] == {"tokens": None, "cost_usd": "0.05"}


def test_inclusive_usage_is_not_silently_counted_twice_with_nested_work():
    run = make_run(BudgetLimits(max_tokens=20), unknown_usage="monitor_only")
    child = run.wrap(
        lambda: "child", boundary="model", dispatch=bounds(5), usage_reader=lambda _: usage(5, 0.05)
    )

    def reader(_):
        return usage(5, 0.05, exclusive=False)

    run.wrap(
        child, boundary="model", branch_id="parent", dispatch=bounds(10), usage_reader=reader
    )()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["tokens_known"] == 5
    assert snapshot["unknown_usage"]["tokens"] == 1
    assert snapshot["total_usage"]["tokens"] is None


def test_identical_run_labels_do_not_hide_fresh_budget_scopes():
    harness = Harness(HarnessConfig("enforce", (budget_policy(BudgetLimits(max_tool_calls=1)),)))
    first, second = harness.start_run("same-label"), harness.start_run("same-label")
    for run in (first, second):
        run.wrap(lambda: None, boundary="tool")()
    assert last_snapshot(first)["budget_scope_id"] != last_snapshot(second)["budget_scope_id"]


def test_independent_budget_policies_in_one_run_have_distinct_scope_identities():
    policies = (
        budget_policy(BudgetLimits(max_model_calls=1), policy_id="calls"),
        budget_policy(BudgetLimits(max_tokens=10), policy_id="tokens"),
    )
    run = Harness(HarnessConfig("enforce", policies)).start_run()
    run.wrap(lambda: None, boundary="model", dispatch=bounds(10), usage_reader=lambda _: usage(5))()
    scopes = {
        proposal.decision.budget_snapshot.to_dict()["budget_scope_id"]
        for proposal in run.results[-1].proposals
    }
    assert len(scopes) == 2


def test_clock_exception_after_dispatch_does_not_lose_committed_usage():
    readings = iter([0, RuntimeError("private clock failure")])

    def clock():
        reading = next(readings)
        if isinstance(reading, Exception):
            raise reading
        return reading

    run = make_run(BudgetLimits(deadline_at=10, max_tokens=10), clock=clock)
    with pytest.raises(HarnessEscalationError):
        run.wrap(
            lambda: None,
            boundary="model",
            dispatch=bounds(10),
            usage_reader=lambda _: usage(5, 0.05),
        )()
    snapshot = last_snapshot(run)
    assert snapshot["committed"]["model_calls"] == 1 and snapshot["total_usage"]["tokens"] == 5
    assert snapshot["in_flight"]["model_calls"] == 0
    assert "clock_invalid" in snapshot["status_codes"]


def test_budget_snapshot_round_trips_without_raw_response_data():
    import json

    from agentloop.harness_evidence import read_evidence
    from agentloop.otel import trace_from_otel, trace_to_otel
    from agentloop.tracer import AgentTrace, trace_agent

    with trace_agent("budget-roundtrip", metadata={"synthetic": True}) as trace:
        run = make_run(BudgetLimits(max_tokens=10, max_cost_usd=0.1))
        result = {"private": "private response", "usage": usage(5, 0.05)}
        assert (
            run.wrap(
                lambda: result,
                boundary="model",
                dispatch=bounds(10, 0.1),
                usage_reader=lambda reply: reply["usage"],
            )()
            is result
        )
    expected = read_evidence(trace)
    assert "private response" not in json.dumps(expected)
    assert read_evidence(AgentTrace.from_dict(json.loads(json.dumps(trace.to_dict())))) == expected
    assert read_evidence(trace_from_otel(trace_to_otel(trace))) == expected


def test_collectors_cannot_dispatch_nested_protected_work():
    run = make_run(BudgetLimits(max_tokens=10), mode="shadow")

    def reader(_):
        run.wrap(lambda: pytest.fail("collector dispatched a model"), boundary="model")()

    assert (
        run.wrap(lambda: "original", boundary="model", dispatch=bounds(10), usage_reader=reader)()
        == "original"
    )
    assert last_snapshot(run)["usage_error"] == "collector_error"


def test_budget_does_not_reroute_model_or_rewrite_context():
    import json

    run = make_run(BudgetLimits(max_tokens=10))
    messages = [{"role": "user", "content": "private input context"}]
    output = {"text": "private model output"}

    def model(*, model_name, context):
        assert model_name == "requested-model" and context is messages
        return output

    wrapped = run.wrap(
        model, boundary="model", dispatch=bounds(10), usage_reader=lambda _: usage(5)
    )
    assert wrapped(model_name="requested-model", context=messages) is output
    artifact = json.dumps(run.export_evidence())
    assert "private input context" not in artifact and "private model output" not in artifact


def test_shadow_policy_cannot_claim_enforced_budget_evidence():
    from agentloop.budget_types import BudgetSnapshot

    source = make_run(BudgetLimits(max_tokens=10))
    source.wrap(
        lambda: None, boundary="model", dispatch=bounds(10), usage_reader=lambda _: usage(5)
    )()
    snapshot = BudgetSnapshot.from_dict(last_snapshot(source))
    policy = Policy("invalid-claim", "1", lambda context: Decision(budget_snapshot=snapshot))
    shadow = Harness(HarnessConfig("shadow", (policy,))).start_run()
    assert shadow.wrap(lambda: "unchanged", boundary="model")() == "unchanged"
    assert shadow.results[0].proposals[0].failed is True


def test_budget_artifacts_reject_hard_spend_guarantees():
    from agentloop.budget_types import BudgetSnapshot

    run = make_run(BudgetLimits(max_tool_calls=1))
    run.wrap(lambda: None, boundary="tool")()
    snapshot = last_snapshot(run)
    snapshot["hard_spend_cap"] = True
    with pytest.raises(ValueError, match="guarantee"):
        BudgetSnapshot.from_dict(snapshot)


def test_async_clock_is_rejected_before_use():
    async def clock():
        return 0

    with pytest.raises(ValueError, match="synchronous"):
        budget_policy(BudgetLimits(deadline_at=10), clock=clock)
