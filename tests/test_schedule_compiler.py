"""Compile recorded tool calls into a bounded concurrent schedule (issue #400)."""

from __future__ import annotations

import threading
import time

import pytest

from agentloop.events import AgentEvent
from agentloop.execution_ir import EFFECTS_KEY
from agentloop.harness import Harness, HarnessConfig
from agentloop.schedule_compiler import compile_schedule, tool_calls
from agentloop.scheduling_types import ScheduleConfig
from agentloop.timing_sensitivity import compare_prediction
from agentloop.tool_scheduling import ToolScheduler, scheduling_policy
from agentloop.tracer import AgentTrace


def tool(event_id, start, end, *, deps=(), effect="read", reads=(), writes=None, parent="agent",
         extra=None):  # fmt: skip
    metadata = {"depends_on": list(deps) if deps is not None else None}
    if deps is None:
        metadata.pop("depends_on")
    effects = {"effect": effect, "reads": list(reads)} if effect is not None else None
    if effects is not None and writes is not None:
        effects["writes"] = list(writes)
    if effects is not None:
        metadata[EFFECTS_KEY] = effects
    metadata.update(extra or {})
    return AgentEvent(
        event_id=event_id,
        run_id="run",
        event_type="tool_call",
        name=event_id,
        started_at=f"2026-01-01T00:00:{start:06.3f}+00:00",
        ended_at=f"2026-01-01T00:00:{end:06.3f}+00:00",
        duration_ms=round((end - start) * 1000, 3),
        parent_id=parent,
        metadata=metadata,
    )


def recorded(*tools):
    trace = AgentTrace(name="t", run_id="run")
    trace.add_event(
        AgentEvent(
            event_id="agent",
            run_id="run",
            event_type="model_call",
            name="agent",
            started_at="2026-01-01T00:00:00.000+00:00",
            ended_at="2026-01-01T00:00:59.000+00:00",
            duration_ms=59000.0,
        )
    )
    for item in tools:
        trace.add_event(item)
    return trace


def three_serial_reads():
    return recorded(
        tool("a", 1, 2, reads=["catalog"]),
        tool("b", 2, 3, reads=["catalog"]),
        tool("c", 3, 4, reads=["prices"]),
    )


def only_group(result):
    (group,) = result["groups"]
    return group


def test_independent_declared_reads_become_a_candidate():
    group = only_group(compile_schedule(three_serial_reads(), max_concurrency=4))
    assert group["status"] == "candidate"
    assert group["observed_ms"] == 3000
    assert group["predicted_ms"] == 1000
    assert group["predicted_gain_ms"] == 2000
    assert group["evidence"] == "simulated"
    assert group["effective_dependencies"] == {"a": [], "b": [], "c": []}


def test_concurrency_bound_limits_the_prediction():
    group = only_group(compile_schedule(three_serial_reads(), max_concurrency=2))
    assert group["predicted_ms"] == 2000
    with pytest.raises(ValueError):
        compile_schedule(three_serial_reads(), max_concurrency=0)


def test_shared_mutable_state_is_serialized():
    group = only_group(
        compile_schedule(
            recorded(
                tool("w", 1, 2, effect="write", writes=["cart"]),
                tool("r", 2, 3, reads=["cart"]),
                tool("x", 3, 4, reads=["prices"]),
            )
        )
    )
    assert group["status"] == "candidate"
    assert group["effective_dependencies"]["r"] == ["w"]
    assert group["resource_conflicts"] == [{"before": "w", "after": "r", "resources": ["cart"]}]
    assert group["predicted_ms"] == 2000


def test_write_write_conflict_without_order_abstains():
    group = only_group(
        compile_schedule(
            recorded(
                tool("w1", 1, 2, effect="write", writes=["cart"]),
                tool("w2", 2, 3, effect="write", writes=["cart"]),
            )
        )
    )
    assert group["status"] == "abstained"
    assert any("conflicting writes" in reason for reason in group["reasons"])


def test_declared_chain_has_no_gain():
    group = only_group(compile_schedule(recorded(tool("a", 1, 2), tool("b", 2, 3, deps=["a"]))))
    assert group["status"] == "no_gain"
    assert group["predicted_gain_ms"] == 0


def test_dependency_cycle_abstains():
    group = only_group(
        compile_schedule(recorded(tool("a", 1, 2, deps=["b"]), tool("b", 2, 3, deps=["a"])))
    )
    assert group["status"] == "abstained"


@pytest.mark.parametrize(
    ("events", "fragment"),
    [
        ((tool("a", 1, 2, deps=None), tool("b", 2, 3)), "dependencies are not declared"),
        ((tool("a", 1, 2, effect=None), tool("b", 2, 3)), "effect is not declared"),
        ((tool("a", 1, 2, effect="external"), tool("b", 2, 3)), "cannot be scheduled"),
        ((tool("a", 1, 2, effect="write"), tool("b", 2, 3)), "must declare the state keys"),
        ((tool("a", 1, 2, extra={"parallel_safe": False}), tool("b", 2, 3)), "parallel_safe"),
    ],
)
def test_unknown_evidence_abstains_never_infers_from_timing(events, fragment):
    group = only_group(compile_schedule(recorded(*events)))
    assert group["status"] == "abstained"
    assert any(fragment in reason for reason in group["reasons"]), group["reasons"]


def test_groups_are_per_parent_and_singletons_are_skipped():
    result = compile_schedule(
        recorded(tool("a", 1, 2), tool("b", 2, 3), tool("solo", 4, 5, parent=None))
    )
    assert [group["parent"] for group in result["groups"]] == ["agent"]
    assert result["candidates"] == 1


# Execution through the existing scheduler --------------------------------------------


def scheduler(max_concurrency=4, on_error="stop"):
    settings = ScheduleConfig("compiled", "1", "tools", max_concurrency=max_concurrency,
                              on_error=on_error)  # fmt: skip
    run = Harness(
        HarnessConfig(mode="enforce", policies=(scheduling_policy(settings),))
    ).start_run()
    return ToolScheduler(run, config=settings)


def test_compiled_calls_run_concurrently_and_keep_result_order():
    group = only_group(compile_schedule(three_serial_reads()))
    running, peak, lock = [0], [0], threading.Lock()

    def make(name):
        def invoke(context):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return name

        return invoke

    result = scheduler().execute(tool_calls(group, {n: make(n) for n in "abc"}))
    assert result.completed
    assert [o.call_id for o in result.outcomes] == ["a", "b", "c"]
    assert [o.value for o in result.outcomes] == ["a", "b", "c"]
    assert peak[0] >= 2


def test_rate_limit_is_respected():
    group = only_group(compile_schedule(three_serial_reads(), max_concurrency=1))
    running, peak, lock = [0], [0], threading.Lock()

    def invoke(context):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1

    result = scheduler(max_concurrency=1).execute(tool_calls(group, dict.fromkeys("abc", invoke)))
    assert result.completed and peak[0] == 1


def test_partial_failure_stops_dependents():
    group = only_group(
        compile_schedule(
            recorded(
                tool("w", 1, 2, effect="write", writes=["cart"]),
                tool("r", 2, 3, reads=["cart"]),
            )
        )
    )

    def broken(context):
        raise RuntimeError("write failed")

    result = scheduler().execute(tool_calls(group, {"w": broken, "r": lambda context: "read"}))
    assert not result.completed
    statuses = {o.call_id: o.status for o in result.outcomes}
    assert statuses["w"] == "failed" and statuses["r"] != "completed"


def test_timeout_is_cooperative():
    group = only_group(compile_schedule(three_serial_reads(), max_concurrency=1))

    def slow(context):
        time.sleep(0.2)

    result = scheduler(max_concurrency=1).execute(
        tool_calls(group, dict.fromkeys("abc", slow)), timeout_s=0.1
    )
    assert not result.completed
    assert result.stop_reason is not None


def test_tool_calls_needs_every_callable_and_a_candidate():
    group = only_group(compile_schedule(three_serial_reads()))
    with pytest.raises(ValueError, match="no callable"):
        tool_calls(group, {"a": lambda context: 1})
    with pytest.raises(ValueError):
        tool_calls({**group, "status": "abstained"}, {})


def test_predicted_gain_matches_measured_runs():
    group = only_group(compile_schedule(three_serial_reads()))
    unit = 0.03  # recorded durations are 1 s; scale them down for the test

    def sleeper(context):
        time.sleep(unit)

    def timed(run):
        start = time.perf_counter()
        run()
        return (time.perf_counter() - start) * 1000

    def serial():
        for _ in range(3):
            sleeper(None)

    def concurrent():
        scheduler().execute(tool_calls(group, dict.fromkeys("abc", sleeper)))

    baseline = [timed(serial) for _ in range(5)]
    candidate = [timed(concurrent) for _ in range(5)]
    predicted = group["predicted_gain_ms"] * unit
    check = compare_prediction(predicted, baseline, candidate)
    assert check["measured_effect_detected"] is True
    assert check["measured_gain_ms"] > 0
    # Thread start-up costs a little; the prediction should be close, not exact.
    assert abs(check["prediction_error_ms"]) < predicted


def test_accepts_prebuilt_ir():
    from agentloop.execution_ir import build_execution_ir

    trace = three_serial_reads()
    assert compile_schedule(build_execution_ir(trace)) == compile_schedule(trace)
