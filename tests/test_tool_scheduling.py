from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock, get_ident

import pytest

from agentloop import current_trace, trace_agent
from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig, HarnessControlError
from agentloop.scheduling_plan import plan_tools
from agentloop.scheduling_types import SCHEDULE_KEY, ScheduleConfig, SchedulingError, ToolCall
from agentloop.tool_scheduling import ToolScheduler, scheduling_policy


def config(**changes):
    return replace(ScheduleConfig("schedule", "1", "tools", max_concurrency=2), **changes)


def scheduler(mode="enforce", settings=None, budgets=()):
    settings = settings or config()
    run = Harness(
        HarnessConfig(mode=mode, policies=(scheduling_policy(settings), *budgets))
    ).start_run()
    return ToolScheduler(run, config=settings)


def call(identity, function=None, **changes):
    return ToolCall(
        identity,
        function or (lambda context: identity),
        depends_on=changes.pop("depends_on", ()),
        reads=changes.pop("reads", ()),
        writes=changes.pop("writes", ()),
        concurrent=changes.pop("concurrent", True),
        effect=changes.pop("effect", "read_only"),
        **changes,
    )


def record(value):
    return next(iter(value.export_evidence()["records"].values()))


def test_fanout_joins_dependencies_and_returns_submission_order_with_trace_context():
    ready = Barrier(2)
    with trace_agent("schedule") as trace:

        def leaf(context):
            assert current_trace() is trace
            ready.wait(timeout=3)
            return context.call_id

        value = scheduler()
        result = value.execute(
            [
                call(
                    "join",
                    lambda context: context.prerequisites["a"] + context.prerequisites["b"],
                    depends_on=("a", "b"),
                ),
                call("a", leaf),
                call("b", leaf),
            ]
        )
    assert result.completed
    assert [item.call_id for item in result.outcomes] == ["join", "a", "b"]
    assert [item.value for item in result.outcomes] == ["ab", "a", "b"]
    evidence = record(value)
    assert evidence["peak_running"] == 2 and evidence["dispatch_order"][-1] == "join"
    assert trace.metadata[SCHEDULE_KEY] == value.export_evidence()


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_disabled_and_shadow_remain_serial_in_calling_thread(mode):
    caller = get_ident()
    visits = []
    value = scheduler(mode)
    result = value.execute(
        [
            call(str(index), lambda context: visits.append((context.call_id, get_ident())))
            for index in range(4)
        ]
    )
    assert result.completed and visits == [(str(index), caller) for index in range(4)]
    assert (
        not value.export_evidence()["records"]
        if mode == "disabled"
        else record(value)["peak_running"] == 1
    )


def test_unknown_safety_uses_original_order_without_worker_threads():
    caller = get_ident()
    value = scheduler()
    result = value.execute(
        [
            ToolCall("first", lambda context: get_ident()),
            ToolCall("second", lambda context: context.prerequisites["first"]),
        ]
    )
    assert [item.value for item in result.outcomes] == [caller, caller]
    assert record(value)["plan"]["unknown_safety"] == ["first", "second"]
    assert record(value)["actual_concurrency_limit"] == 1
    strict = scheduler(settings=config(on_unknown="error"))
    with pytest.raises(SchedulingError, match="unknown_safety"):
        strict.execute([ToolCall("opaque", lambda context: pytest.fail("unsafe dispatch"))])


@pytest.mark.parametrize(
    "kind", ["duplicate", "missing", "cycle", "duplicate_edge", "unknown_forward"]
)
def test_invalid_dags_never_dispatch(kind):
    calls = []

    def invoke(context):
        calls.append(context.call_id)

    value = scheduler()
    with pytest.raises(SchedulingError):
        if kind == "duplicate":
            value.execute([call("a", invoke), call("a", invoke)])
        elif kind == "missing":
            value.execute([call("a", invoke, depends_on=("absent",))])
        elif kind == "cycle":
            value.execute(
                [call("a", invoke, depends_on=("b",)), call("b", invoke, depends_on=("a",))]
            )
        elif kind == "duplicate_edge":
            value.execute([call("a", invoke), call("b", invoke, depends_on=("a", "a"))])
        else:
            value.execute([call("a", invoke, depends_on=("b",)), ToolCall("b", invoke)])
    assert not calls


def test_shared_writes_are_serialized_without_name_based_safety_inference():
    effects = []
    calls = [
        call(
            str(index),
            lambda context: effects.append(context.call_id) or len(effects),
            writes=("shared-counter",),
            effect="mutating",
        )
        for index in range(5)
    ]
    value = scheduler(settings=config(max_concurrency=4))
    result = value.execute(calls)
    assert effects == [str(index) for index in range(5)]
    assert [item.value for item in result.outcomes] == [1, 2, 3, 4, 5]
    assert record(value)["peak_running"] == 1
    assert len(record(value)["plan"]["resource_conflicts"]) == 10


def test_resource_serialization_preserves_declared_reverse_dependency_order():
    calls = [
        call("later", depends_on=("first",), reads=("shared",)),
        call("first", writes=("shared",), effect="mutating"),
    ]
    plan = plan_tools(calls, config())
    assert plan["serial_order"] == ["first", "later"]
    assert plan["effective_dependencies"]["later"] == ["first"]
    assert scheduler().execute(calls).completed


def test_budget_races_never_invoke_more_tools_than_admitted():
    visits = []
    lock = Lock()

    def invoke(context):
        with lock:
            visits.append(context.call_id)
        return context.call_id

    value = scheduler(
        settings=config(max_concurrency=4, on_error="continue_independent"),
        budgets=(budget_policy(BudgetLimits(max_tool_calls=2)),),
    )
    result = value.execute([call(str(index), invoke) for index in range(8)])
    assert len(visits) == 2 and not result.completed
    assert sum(item.status == "completed" for item in result.outcomes) == 2
    assert all(record(value)["calls"][identity]["callback_invoked"] for identity in visits)


def test_error_can_continue_only_independent_work_and_never_repeats_mutation():
    effects = []

    def broken(context):
        effects.append("changed")
        raise ValueError("PRIVATE tool error")

    value = scheduler(settings=config(on_error="continue_independent"))
    calls = [
        call("mutate", broken, writes=("shared",), effect="mutating"),
        call("dependent", depends_on=("mutate",)),
        call("independent"),
    ]
    result = value.execute(calls)
    assert [item.status for item in result.outcomes] == ["failed", "dependency_failed", "completed"]
    assert effects == ["changed"] and "PRIVATE" not in str(value.export_evidence())
    with pytest.raises(SchedulingError, match="already_started"):
        value.execute(calls)
    assert effects == ["changed"]


def test_already_running_batch_cannot_be_submitted_again():
    entered, release = Event(), Event()

    def invoke(context):
        entered.set()
        assert release.wait(3)
        return "done"

    value = scheduler()
    calls = [call("once", invoke)]
    with ThreadPoolExecutor(max_workers=1) as host:
        future = host.submit(value.execute, calls)
        try:
            assert entered.wait(3)
            with pytest.raises(SchedulingError, match="already_started"):
                value.execute(calls)
        finally:
            release.set()
        assert future.result(timeout=3).outcomes[0].value == "done"


def test_cancel_stops_new_admission_but_retains_completed_result():
    cancel = Event()

    def first(context):
        cancel.set()
        return "completed effect"

    value = scheduler(settings=config(max_concurrency=1))
    result = value.execute(
        [call("a", first), call("b", lambda context: pytest.fail("admitted after cancel"))],
        cancel_event=cancel,
    )
    assert not result.completed and result.stop_reason == "cancelled"
    assert (
        result.outcomes[0].value == "completed effect"
        and result.outcomes[1].status == "not_started"
    )


def test_deadline_is_cooperative_and_drains_inflight_work(monkeypatch):
    import agentloop.scheduling_types as types
    import agentloop.tool_scheduling as scheduling

    now = [0.0]
    monkeypatch.setattr(types, "monotonic", lambda: now[0])
    monkeypatch.setattr(scheduling, "monotonic", lambda: now[0])
    entered, release = Event(), Event()

    def invoke(context):
        entered.set()
        assert release.wait(3)
        assert context.cancellation_requested
        return "late but completed"

    value = scheduler()
    with ThreadPoolExecutor(max_workers=1) as host:
        future = host.submit(
            value.execute,
            [
                call("a", invoke),
                call("b", lambda context: pytest.fail("late dispatch"), depends_on=("a",)),
            ],
            timeout_s=1,
        )
        try:
            assert entered.wait(3)
            now[0] = 2.0
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=3)
    assert result.stop_reason == "deadline" and not result.completed
    assert result.outcomes[0].value == "late but completed"
    assert result.outcomes[1].status == "not_started"


def test_after_hook_stop_does_not_lose_completed_mutating_work():
    effects = []
    value = scheduler(
        settings=config(max_concurrency=1),
        budgets=(budget_policy(BudgetLimits(max_tokens=1), metered_boundaries=("tool",)),),
    )
    work = call(
        "write",
        lambda context: effects.append(1) or "saved",
        writes=("record",),
        effect="mutating",
        dispatch=DispatchOptions(Reservation(tokens=1, provenance="upper_bound")),
        usage_reader=lambda result: ResourceUsage(
            tokens=2, token_provenance="user_supplied", complete=True
        ),
    )
    result = value.execute(
        [work, call("later", lambda context: pytest.fail("continued after overrun"))]
    )
    assert effects == [1] and result.outcomes[0].value == "saved"
    assert isinstance(result.outcomes[0].error, HarnessControlError)
    assert record(value)["calls"]["write"]["callback_completed"]
    assert not result.completed and result.outcomes[1].status == "not_started"


def test_worker_interrupt_preserves_primary_exception_and_partial_results():
    error = KeyboardInterrupt()

    def interrupt(context):
        raise error

    value = scheduler(settings=config(max_concurrency=1))
    with pytest.raises(KeyboardInterrupt) as caught:
        value.execute(
            [call("first", lambda context: "kept"), call("cancel", interrupt), call("later")]
        )
    assert caught.value is error
    assert value.last_result.outcomes[0].value == "kept"
    assert value.last_result.outcomes[2].status == "not_started"


def test_strict_shadow_records_unsafe_proposal_without_changing_original_execution():
    value = scheduler("shadow", settings=config(on_unknown="error"))
    result = value.execute([ToolCall("opaque", lambda context: "unchanged")])
    assert result.completed and result.outcomes[0].value == "unchanged"
    assert any(hook.action == "deny" and not hook.applied for hook in value.run.results)


def test_executor_setup_failure_releases_scheduler_and_retains_unstarted_slots(monkeypatch):
    import agentloop.tool_scheduling as scheduling

    error = RuntimeError("PRIVATE executor setup")

    def unavailable(**kwargs):
        raise error

    value = scheduler()
    monkeypatch.setattr(scheduling, "ThreadPoolExecutor", unavailable)
    with pytest.raises(RuntimeError) as caught:
        value.execute([call("a"), call("b")])
    assert caught.value is error
    assert all(item.status == "not_started" for item in value.last_result.outcomes)
    assert not value.last_result.completed and "PRIVATE" not in str(value.export_evidence())
    with pytest.raises(SchedulingError, match="already_started"):
        value.execute([call("a")])


def test_pool_submission_failure_cannot_lose_already_completed_mutation(monkeypatch):
    import agentloop.tool_scheduling as scheduling

    class FailingPool:
        def __init__(self, **kwargs):
            self.pool = ThreadPoolExecutor(max_workers=1)
            self.submitted = 0

        def submit(self, function, *args):
            self.submitted += 1
            if self.submitted == 2:
                raise RuntimeError("submission unavailable")
            future = self.pool.submit(function, *args)
            future.result(timeout=3)
            return future

        def shutdown(self, **kwargs):
            self.pool.shutdown(**kwargs)

    monkeypatch.setattr(scheduling, "ThreadPoolExecutor", FailingPool)
    changes = []
    value = scheduler()
    with pytest.raises(RuntimeError, match="submission"):
        value.execute(
            [
                call(
                    "done",
                    lambda context: changes.append(1) or "saved",
                    effect="mutating",
                    writes=("record",),
                ),
                call("pending"),
            ]
        )
    assert changes == [1] and value.last_result.outcomes[0].value == "saved"
    assert value.last_result.outcomes[1].status == "not_started"


def test_evidence_capacity_and_owned_exports_prevent_silent_history_loss():
    settings = config()
    run = Harness(
        HarnessConfig(mode="enforce", policies=(scheduling_policy(settings),))
    ).start_run()
    value = ToolScheduler(run, config=settings, max_records=1)
    value.execute([call("a", lambda context: "PRIVATE output")])
    copied = value.export_evidence()
    copied["records"].clear()
    assert value.export_evidence()["records"]
    assert "PRIVATE" not in str(value.export_evidence())
    with pytest.raises(SchedulingError, match="evidence_limit"):
        value.execute([call("b", lambda context: pytest.fail("unrecorded work"))])
