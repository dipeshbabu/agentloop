from __future__ import annotations

import pytest

from agentloop.ci import build_ci_report
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.tracer import AgentTrace


def trace(name, elapsed, **capture):
    return AgentTrace(
        name,
        metadata={
            "integration": "hermes",
            "hermes": {"capture_complete": True, "turn_outcome": "completed", **capture},
        },
        started_at="2026-01-01T00:00:00+00:00",
        ended_at=f"2026-01-01T00:00:00.{int(elapsed * 1000):06d}+00:00",
        elapsed_ms=elapsed,
    )


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize(
    "capture",
    [
        {"capture_complete": False},
        {"turn_outcome": "interrupted"},
        {"turn_outcome": "failed"},
        {"capture_complete": "true"},
        {"unfinished_requests": 1},
        {"events_dropped": 1},
    ],
)
def test_hermes_partial_or_unfinished_work_cannot_pass_a_performance_gate(side, capture):
    baseline = trace("baseline", 100, **(capture if side == "baseline" else {}))
    candidate = trace("candidate", 10, **(capture if side == "candidate" else {}))
    gates = ReplayGates(min_latency_improvement_pct=20)
    replay = build_replay_report(baseline, candidate, gates=gates)
    assert replay["gates"]["passed"] is False
    assert build_ci_report(baseline, candidate, gates=gates)["passed"] is False


def test_complete_hermes_turns_keep_the_existing_gate_behavior():
    assert build_replay_report(trace("before", 100), trace("after", 10))["gates"]["passed"] is True
