from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from threading import Event
from types import SimpleNamespace

import pytest

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.checkpoint_state import CheckpointError, restore_run, seal_run
from agentloop.checkpoint_store import MemoryRecoveryStore
from agentloop.harness import Decision, Harness, HarnessConfig, HarnessControlError, Policy
from agentloop.integrations.langgraph_harness import LangGraphHarness
from agentloop.integrations.langgraph_recovery import LangGraphRecovery
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint
from agentloop.recovery_evidence import RECOVERY_KEY, recovery_status


def harness(*policies):
    return Harness(
        HarnessConfig("enforce", policies or (budget_policy(BudgetLimits(max_model_calls=2)),))
    )


def resume_state(run, *, factory=None, domain="clock", run_id="resumed"):
    payload = seal_run(run, clock_domain="clock")
    return restore_run(factory or run.harness, payload, clock_domain=domain, run_id=run_id)


def test_model_tool_and_retry_quota_survive_new_attempt_and_original_is_retired():
    factory = harness(
        budget_policy(BudgetLimits(max_model_calls=1, max_tool_calls=1, max_retries=1))
    )
    old = factory.start_run("original")
    old.wrap(lambda: 1, boundary="model", dispatch=DispatchOptions(retry_source="harness"))()
    old.wrap(lambda: 2, boundary="tool")()
    new = resume_state(old)
    for run, boundary in ((old, "model"), (new, "model"), (new, "tool")):
        with pytest.raises(HarnessControlError):
            run.wrap(lambda: pytest.fail("quota reset"), boundary=boundary)()
    assert new.run_id != old.run_id
    assert (
        new.results[0].proposals[0].decision.budget_snapshot.to_dict()["committed"]["model_calls"]
        == 1
    )


def test_exact_money_unknown_usage_and_usage_deduplication_survive_json_storage():
    factory = harness(budget_policy(BudgetLimits(max_cost_usd=0.3, max_tokens=30)))
    old = factory.start_run("original")
    usage = ResourceUsage(
        tokens=10,
        cost_usd=0.1,
        token_provenance="user_supplied",
        cost_provenance="user_reported",
        complete=True,
        usage_id="usage1",
    )
    dispatch = DispatchOptions(Reservation(10, 0.1, "upper_bound", True))
    old.wrap(lambda: 1, boundary="model", dispatch=dispatch, usage_reader=lambda value: usage)()
    encoded = json.dumps(seal_run(old, clock_domain="clock"))
    new = restore_run(factory, json.loads(encoded), clock_domain="clock", run_id="new")
    new.wrap(
        lambda: 2,
        boundary="model",
        dispatch=dispatch,
        usage_reader=lambda value: replace(usage, usage_id="usage2"),
    )()
    snapshot = new.results[-1].proposals[0].decision.budget_snapshot.to_dict()
    assert snapshot["committed"]["cost_known_usd"] == "0.2"
    assert snapshot["committed"]["tokens_known"] == 20
    assert new._states["budget"]["budget"].seen_usage.keys() == {"usage1", "usage2"}


def test_unknown_usage_is_not_reset_to_known_zero():
    old = harness(
        budget_policy(BudgetLimits(max_model_calls=3), unknown_usage="monitor_only")
    ).start_run("original")
    old.wrap(lambda: 1, boundary="model")()
    new = resume_state(old)
    new.wrap(lambda: 2, boundary="model")()
    snapshot = new.results[-1].proposals[0].decision.budget_snapshot.to_dict()
    assert snapshot["unknown_usage"]["calls"] == 2
    assert snapshot["total_usage"]["cost_usd"] is None


def test_loop_history_and_retry_limits_survive_recovery():
    old = harness(
        budget_policy(BudgetLimits(max_model_calls=10)),
        loop_guard_policy(LoopLimits(max_identical_calls=1)),
    ).start_run("old")
    dispatch = DispatchOptions(
        step=StepInfo("same", fingerprint(1), fingerprint(0), mutating=False)
    )
    old.wrap(lambda: 1, boundary="model", dispatch=dispatch)()
    new = resume_state(old)
    with pytest.raises(HarnessControlError):
        new.wrap(lambda: pytest.fail("lost history"), boundary="model", dispatch=dispatch)()


def test_in_flight_work_is_not_sealed():
    entered, release = Event(), Event()
    old = harness().start_run()

    def work():
        entered.set()
        assert release.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(old.wrap(work, boundary="model"))
        try:
            assert entered.wait(timeout=3)
            with pytest.raises(CheckpointError, match="in_flight"):
                seal_run(old, clock_domain="clock")
            assert not old.stopped
        finally:
            release.set()
        future.result(timeout=3)


def test_stopped_runs_and_unsupported_policies_cannot_reset_controls():
    old = harness(
        loop_guard_policy(LoopLimits(max_retries_per_step=0)), budget_policy(BudgetLimits())
    ).start_run()
    with pytest.raises(HarnessControlError):
        old.wrap(
            lambda: None,
            boundary="model",
            dispatch=DispatchOptions(
                retry_source="harness", step=StepInfo("retry", mutating=False)
            ),
        )()
    with pytest.raises(CheckpointError, match="stopped_run"):
        seal_run(old, clock_domain="clock")
    other = harness(
        budget_policy(BudgetLimits()), Policy("custom", "1", lambda context: Decision())
    ).start_run()
    with pytest.raises(CheckpointError, match="unsupported_policy"):
        seal_run(other, clock_domain="clock")


@pytest.mark.parametrize("change", ["schema", "config", "clock", "run_id", "codec"])
def test_invalid_restore_is_rejected(change):
    factory = harness(budget_policy(BudgetLimits(deadline_at=100), clock=lambda: 1))
    old = factory.start_run("old")
    payload = seal_run(old, clock_domain="clock")
    domain, identity = "clock", "new"
    if change == "schema":
        payload["schema_version"] = "2"
    elif change == "config":
        payload["config_hash"] = "different"
    elif change == "clock":
        domain = "rebooted"
    elif change == "run_id":
        identity = "old"
    else:
        payload["states"] = {"pickle": "forbidden"}
    with pytest.raises(CheckpointError):
        restore_run(factory, payload, clock_domain=domain, run_id=identity)


def test_past_absolute_deadline_is_not_extended_on_resume():
    clock = [1]
    factory = harness(budget_policy(BudgetLimits(deadline_at=10), clock=lambda: clock[0]))
    old = factory.start_run("old")
    old.wrap(lambda: 1, boundary="model")()
    payload = seal_run(old, clock_domain="clock")
    clock[0] = 11
    new = restore_run(factory, payload, clock_domain="clock", run_id="new")
    with pytest.raises(HarnessControlError):
        new.wrap(lambda: pytest.fail("deadline restarted"), boundary="model")()


def test_store_atomic_claim_and_high_water_mark():
    store = MemoryRecoveryStore()
    record = {"owned": "private state"}
    ref = store.save("owner", "thread", record)
    record["owned"] = "mutated"
    digest = fingerprint(store.load("owner", "thread", ref))
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(
                lambda index: store.claim("owner", "thread", ref, str(index), digest), range(4)
            )
        )
    assert sum(claims) == 1
    with pytest.raises(CheckpointError, match="owner"):
        store.load("other", "thread", ref)
    with pytest.raises(CheckpointError, match="stale"):
        store.save("owner", "thread", record)
    request = str(claims.index(True))
    newest = store.save("owner", "thread", record, previous_ref=ref, request_id=request)
    assert not store.claim("owner", "thread", ref, "later", digest)
    assert not store.claim("owner", "thread", newest, request, fingerprint(record))


class FakeGraph:
    checkpointer = object()

    def __init__(self, adapter):
        self.call = adapter.model(lambda: 42)
        self.checkpoint_id = "cp1"
        self.pending = ("safe",)
        self.tasks = ()
        self.calls = 0
        self.error = None

    def invoke(self, value, config=None, **kwargs):
        self.calls += 1
        result = self.call()
        if self.error is not None:
            raise self.error
        if value is None:
            self.pending = ()
            self.checkpoint_id = "cp2"
        return {"private": result}

    async def ainvoke(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)

    def stream(self, *args, **kwargs):
        yield self.invoke(*args, **kwargs)

    async def astream(self, *args, **kwargs):
        yield self.invoke(*args, **kwargs)

    def get_state(self, config):
        return SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread",
                    "checkpoint_ns": "",
                    "checkpoint_id": self.checkpoint_id,
                }
            },
            next=self.pending,
            tasks=self.tasks,
            values={"never": "capture this state"},
        )


@pytest.fixture
def fake(monkeypatch):
    from agentloop.integrations import langgraph_harness, langgraph_recovery

    class SignalError(Exception):
        def __init__(self, original):
            self.original = original

    monkeypatch.setattr(langgraph_harness, "_check_version", lambda: None)
    monkeypatch.setattr(langgraph_recovery, "_check_version", lambda: None)
    monkeypatch.setattr(langgraph_harness, "_signal_type", lambda: SignalError)

    def setup(*, max_calls=2, store=None, safe=("safe",)):
        adapter = LangGraphHarness(
            HarnessConfig("enforce", (budget_policy(BudgetLimits(max_model_calls=max_calls)),)),
            boundaries={"model", "tool", "iteration", "completion"},
        )
        graph = FakeGraph(adapter)
        controlled = adapter.runnable(graph)
        controlled.invoke = partial(controlled.invoke, durability="sync")
        journal = store or MemoryRecoveryStore()
        recovery = LangGraphRecovery(
            controlled,
            journal,
            owner="owner",
            thread_id="thread",
            graph_version="test.v1",
            clock_domain="clock",
            retry_safe_nodes=safe,
        )
        return adapter, graph, controlled, recovery, journal

    return setup


def test_fake_framework_resume_owns_lineage_and_retains_cumulative_budget(fake):
    adapter, graph, controlled, recovery, store = fake()
    with trace_agent("first") as first:
        controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
        original = adapter.last_run
        ref = recovery.capture()
    assert recovery_status(first) == "incomplete"
    with trace_agent("second") as second:
        outcome = recovery.resume(ref, request_id="resume1")
    assert outcome.status == "completed" and recovery_status(second) == "completed"
    assert recovery.last_run.run_id != original.run_id
    snapshot = recovery.last_run.results[-1].proposals[0].decision.budget_snapshot.to_dict()
    assert snapshot["committed"]["model_calls"] == 2
    entries = list(second.metadata[RECOVERY_KEY]["records"].values())
    assert entries[0]["parent_run_id"] == original.run_id
    assert "capture this state" not in json.dumps(second.metadata)
    assert "private" not in json.dumps(first.metadata)
    assert graph.calls == 2


def test_resumed_model_is_denied_at_original_limit(fake):
    adapter, graph, controlled, recovery, store = fake(max_calls=1)
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    ref = recovery.capture()
    with trace_agent("resume") as trace, pytest.raises(HarnessControlError):
        recovery.resume(ref, request_id="blocked")
    assert recovery_status(trace) == "stopped"


@pytest.mark.parametrize(
    "kind", ["duplicate", "stale", "unsafe", "interrupt", "missing", "changed_config"]
)
def test_resume_rejections_never_reinvoke_graph(fake, kind):
    adapter, graph, controlled, recovery, store = fake(safe=() if kind == "unsafe" else ("safe",))
    if kind == "interrupt":
        graph.tasks = (
            SimpleNamespace(name="safe", error=None, interrupts=("private",), state=None),
        )
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    ref = recovery.capture()
    if kind == "duplicate":
        record = store.load("owner", recovery._stream, ref)
        assert store.claim("owner", recovery._stream, ref, "original", fingerprint(record))
    elif kind == "stale":
        graph.checkpoint_id = "newer"
    elif kind == "missing":
        ref = "missing"
    elif kind == "changed_config":
        other_adapter, other_graph, other_controlled, recovery, _ = fake(max_calls=5, store=store)
        other_graph.checkpoint_id = graph.checkpoint_id
    with trace_agent("reject") as trace, pytest.raises(CheckpointError):
        recovery.resume(ref, request_id="request")
    assert graph.calls == 1
    assert recovery_status(trace) != "completed"


def test_same_trace_cannot_mislabel_interrupted_attempt_as_completed(fake):
    _, _, controlled, recovery, _ = fake()
    with trace_agent("same"):
        controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
        ref = recovery.capture()
        with pytest.raises(CheckpointError, match="new_trace"):
            recovery.resume(ref, request_id="bad")


def test_original_host_failure_survives_and_claim_is_not_automatically_released(fake):
    from asyncio import CancelledError

    _, graph, controlled, recovery, store = fake()
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    ref = recovery.capture()
    error = CancelledError("host cancellation")
    graph.error = error
    with trace_agent("cancel") as trace, pytest.raises(CancelledError) as caught:
        recovery.resume(ref, request_id="cancel")
    assert caught.value is error and recovery_status(trace) == "cancelled"
    with pytest.raises(CheckpointError, match="duplicate"):
        recovery.resume(ref, request_id="again")


def test_missing_atomic_claim_capability_is_rejected(fake):
    _, _, controlled, _, store = fake()
    store.atomic_claim = False
    with pytest.raises(CheckpointError, match="unsupported_recovery_store"):
        LangGraphRecovery(
            controlled,
            store,
            owner="owner",
            thread_id="thread",
            graph_version="test.v1",
            clock_domain="clock",
            retry_safe_nodes=(),
        )


def test_capture_refuses_an_unrelated_thread_run(fake):
    _, graph, controlled, recovery, _ = fake()
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "other-thread"}})
    with pytest.raises(CheckpointError, match="run_checkpoint_mismatch"):
        recovery.capture()
    assert graph.calls == 1


def test_captured_runnable_cannot_start_a_fresh_budget_for_resume(fake):
    import asyncio

    _, graph, controlled, recovery, _ = fake()
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    recovery.capture()
    with pytest.raises(ValueError, match="checkpoint-owned"):
        controlled.invoke(None)
    with pytest.raises(ValueError, match="checkpoint-owned"):
        asyncio.run(controlled.ainvoke(None))
    with pytest.raises(ValueError, match="checkpoint-owned"):
        list(controlled.stream(None))

    async def consume():
        return [item async for item in controlled.astream(None)]

    with pytest.raises(ValueError, match="checkpoint-owned"):
        asyncio.run(consume())
    assert graph.calls == 1


def test_failed_journal_save_retires_run_and_preserves_primary_failure(fake):
    class BrokenStore(MemoryRecoveryStore):
        def save(self, *args, **kwargs):
            raise RuntimeError("private storage error")

    adapter, _, controlled, recovery, _ = fake(store=BrokenStore())
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    with trace_agent("save-failed") as trace, pytest.raises(RuntimeError, match="private storage"):
        recovery.capture()
    assert adapter.last_run.stopped
    assert "private storage" not in json.dumps(trace.metadata)
    assert recovery_status(trace) == "incomplete"


def test_new_controller_restores_existing_claimable_record(fake):
    adapter, _, controlled, recovery, store = fake()
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    ref = recovery.capture()
    old_id = adapter.last_run.run_id
    _, _, _, fresh, _ = fake(store=store)
    result = fresh.resume(ref, request_id="new-process")
    assert result.status == "completed" and result.run_id != old_id
    snapshot = fresh.last_run.results[-1].proposals[0].decision.budget_snapshot.to_dict()
    assert snapshot["committed"]["model_calls"] == 2


def test_built_in_policy_may_skip_a_boundary_but_in_flight_detection_cannot():
    entered, release = Event(), Event()
    old = harness(budget_policy(BudgetLimits(max_model_calls=3), boundaries={"model"})).start_run()

    def work():
        entered.set()
        assert release.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old.wrap(work, boundary="tool"))
        try:
            assert entered.wait(timeout=3)
            with pytest.raises(CheckpointError, match="in_flight"):
                seal_run(old, clock_domain="clock")
        finally:
            release.set()
        future.result(timeout=3)


def test_changed_graph_version_and_capabilities_are_rejected(fake):
    from agentloop.harness import Hook

    _, _, controlled, recovery, store = fake()
    controlled.invoke({"start": True}, {"configurable": {"thread_id": "thread"}})
    ref = recovery.capture()
    changed = LangGraphRecovery(
        controlled,
        store,
        owner="owner",
        thread_id="thread",
        graph_version="test.v2",
        clock_domain="clock",
        retry_safe_nodes={"safe"},
    )
    with pytest.raises(CheckpointError, match="incompatible_configuration"):
        changed.resume(ref, request_id="changed")
    original = harness(budget_policy(BudgetLimits(max_model_calls=3), boundaries={"model"}))
    payload = seal_run(original.start_run("original"), clock_domain="clock")
    modified = Harness(
        original.config,
        replace(original.capabilities, hooks=frozenset({Hook("model"), Hook("model", "after")})),
    )
    with pytest.raises(CheckpointError, match="incompatible_configuration"):
        restore_run(modified, payload, clock_domain="clock", run_id="different")


def test_capture_requires_explicit_synchronous_durability(fake):
    adapter, _, controlled, recovery, _ = fake()
    controlled.invoke(
        {"start": True}, {"configurable": {"thread_id": "thread"}}, durability="async"
    )
    with pytest.raises(CheckpointError, match="synchronous_durability"):
        recovery.capture()
    assert not adapter.last_run.stopped


def test_codec_rejects_negative_counts_and_unknown_type_tags():
    factory = harness()
    old = factory.start_run("old")
    old.wrap(lambda: 1, boundary="model")()
    payload = seal_run(old, clock_domain="clock")
    budget = payload["states"]["map"][0][1]["map"][0][1]
    budget["fields"]["tokens_known"] = -1
    with pytest.raises(CheckpointError, match="invalid_policy_state"):
        restore_run(factory, payload, clock_domain="clock", run_id="new")
    budget["fields"]["tokens_known"] = 0
    budget["type"] = "module:class"
    with pytest.raises(CheckpointError, match="invalid_policy_state"):
        restore_run(factory, payload, clock_domain="clock", run_id="new")
