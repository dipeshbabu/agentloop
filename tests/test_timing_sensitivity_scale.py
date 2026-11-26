"""Timing sensitivity stays exact while skipping work that cannot change results."""

from __future__ import annotations

import random
import time

import pytest

from agentloop import timing_sensitivity as module
from agentloop.events import AgentEvent
from agentloop.timing_sensitivity import timing_sensitivity
from agentloop.tracer import AgentTrace


def _stamp(seconds: float) -> str:
    whole, frac = divmod(seconds, 1)
    h, rest = divmod(int(whole), 3600)
    m, s = divmod(rest, 60)
    return f"2026-01-01T{h:02d}:{m:02d}:{s:02d}.{int(round(frac * 1e6)):06d}+00:00"


def random_dag(seed: int, size: int = 30) -> AgentTrace:
    """Operations with declared dependencies, timed as a real run would be."""
    rng = random.Random(seed)
    trace = AgentTrace(name="dag", run_id="run")
    finish: dict[str, float] = {}
    for index in range(size):
        candidates = list(finish)
        deps = rng.sample(candidates, k=min(len(candidates), rng.randint(0, 2)))
        start = max((finish[d] for d in deps), default=0.0) + rng.choice([0.0, 0.0, 0.25])
        duration = rng.choice([0.5, 1.0, 1.5, 2.0, 3.0])
        op = f"op{index}"
        finish[op] = start + duration
        trace.add_event(
            AgentEvent(
                event_id=op, run_id="run", event_type="tool_call", name=op,
                started_at=_stamp(start), ended_at=_stamp(start + duration),
                duration_ms=duration * 1000, metadata={"depends_on": deps},
            )
        )  # fmt: skip
    return trace


def _comparable(result):
    return {
        op["id"]: (op.get("gain_if_removed_ms"), op.get("gain_if_scaled_ms"),
                   op.get("critical_path_after_removal"), op.get("bottleneck_after_removal"))
        for op in result["operations"]
    }  # fmt: skip


@pytest.mark.parametrize("seed", range(12))
def test_skipping_operations_with_slack_matches_full_simulation(seed, monkeypatch):
    trace = random_dag(seed)
    fast = timing_sensitivity(trace, max_simulations=10_000)
    assert fast["status"] == "available"
    # Treat every operation as having no slack: everything is simulated.
    monkeypatch.setattr(module, "_SLACK_EPSILON_MS", float("inf"))
    full = timing_sensitivity(trace, max_simulations=10_000)
    assert _comparable(fast) == _comparable(full)


def test_operations_beyond_the_cap_are_reported_not_estimated():
    trace = AgentTrace(name="chain", run_id="run")
    for index in range(10):
        trace.add_event(
            AgentEvent(
                event_id=f"s{index}", run_id="run", event_type="tool_call", name=f"s{index}",
                started_at=_stamp(index), ended_at=_stamp(index + 1), duration_ms=1000.0,
                metadata={"depends_on": [f"s{index - 1}"] if index else []},
            )
        )  # fmt: skip
    result = timing_sensitivity(trace, max_simulations=3)
    assert result["simulations"] == {"without_slack": 10, "simulated": 3, "skipped": 7,
                                     "max_simulations": 3}  # fmt: skip
    skipped = [op for op in result["operations"] if op.get("simulated") is False]
    assert len(skipped) == 7
    assert all(
        "gain_if_removed_ms" not in op and "max_simulations" in op["reason"] for op in skipped
    )
    with pytest.raises(ValueError):
        timing_sensitivity(trace, max_simulations=-1)


def test_large_traces_finish_quickly():
    trace = AgentTrace(name="wide", run_id="run")
    trace.add_event(
        AgentEvent(event_id="root", run_id="run", event_type="model_call", name="agent",
                   started_at=_stamp(0), ended_at=_stamp(2001), duration_ms=2001 * 1000.0)
    )  # fmt: skip
    for index in range(2000):
        trace.add_event(
            AgentEvent(
                event_id=f"t{index}", run_id="run", event_type="tool_call", name="t",
                started_at=_stamp(index + 0.25), ended_at=_stamp(index + 0.75),
                duration_ms=500.0, parent_id="root",
                metadata={"depends_on": [f"t{index - 1}"] if index else []},
            )
        )  # fmt: skip
    started = time.perf_counter()
    result = timing_sensitivity(trace)
    assert result["status"] == "available"
    assert result["simulations"]["simulated"] == 64
    assert time.perf_counter() - started < 10  # about 22 s before this change
