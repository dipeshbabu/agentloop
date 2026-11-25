"""Bounded retry and loop optimization passes (issue #401)."""

from __future__ import annotations

import pytest

from agentloop.events import AgentEvent
from agentloop.execution_ir import EFFECTS_KEY
from agentloop.loop_guards import LoopLimits
from agentloop.retry_passes import (
    classify_retries,
    compare_runs,
    find_loops,
    propose_limits,
    replay_limits,
    shadow_policy,
)
from agentloop.tracer import AgentTrace

_clock = [0]


def call(
    name, *, status="ok", reason=None, error=None, key=None, idempotent=True, kind="tool_call"
):
    _clock[0] += 1
    second = _clock[0]
    metadata = {}
    if reason:
        metadata["retry_reason"] = reason
    if key:
        metadata["tool_reuse_key"] = key
    if idempotent is not None:
        metadata[EFFECTS_KEY] = {"effect": "read", "idempotent": idempotent}
    return AgentEvent(
        event_id=f"evt_{second:04d}",
        run_id="run",
        event_type=kind,
        name=name,
        started_at=f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}+00:00",
        ended_at=f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}.500000+00:00",
        duration_ms=500.0,
        status=status,
        error=error,
        metadata=metadata,
    )


def run(*events):
    trace = AgentTrace(name="t", run_id="run")
    for event in events:
        trace.add_event(event)
    return trace


def groups(trace):
    return classify_retries(trace)["groups"]


# Classification ---------------------------------------------------------------------


def test_transient_network_failure_is_a_legitimate_retry():
    (group,) = groups(run(call("fetch", status="error", reason="timeout"), call("fetch")))
    assert group["cause"] == "transport"
    assert group["verdict"] == "legitimate"
    assert group["outcome"] == "succeeded"


def test_transport_detected_from_error_text():
    (group,) = groups(
        run(call("fetch", status="error", error="ConnectionResetError: peer"), call("fetch"))
    )
    assert group["cause"] == "transport"


def test_semantic_retries_are_legitimate():
    (group,) = groups(
        run(call("answer", status="error", reason="insufficient_answer"), call("answer"))
    )
    assert group["verdict"] == "legitimate"


def test_repeated_schema_repair_is_a_candidate():
    trace = run(
        call("plan", status="error", reason="invalid_output_schema"),
        call("plan", status="error", reason="invalid_output_schema"),
        call("plan"),
    )
    (group,) = groups(trace)
    assert group["cause"] == "schema" and group["retries"] == 2
    assert group["verdict"] == "candidate"
    assert group["retry_ms"] == 1000


def test_one_schema_retry_is_not_waste():
    (group,) = groups(
        run(call("plan", status="error", reason="invalid_output_schema"), call("plan"))
    )
    assert group["verdict"] == "unknown"


def test_repeated_tool_error_is_a_candidate():
    (group,) = groups(
        run(
            call("lookup", status="error", reason="invalid_arguments"),
            call("lookup", status="error"),
        )
    )
    assert group["verdict"] == "candidate" and group["outcome"] == "failed"


def test_non_idempotent_tools_are_always_flagged():
    (group,) = groups(
        run(
            call("charge", status="error", reason="timeout", idempotent=False),
            call("charge", idempotent=False),
        )
    )
    assert group["verdict"] == "unsafe_retry"
    assert group["unsafe"] is True
    (undeclared,) = groups(
        run(call("charge", status="error", reason="timeout", idempotent=None), call("charge"))
    )
    assert undeclared["verdict"] == "unsafe_retry"


def test_successful_repeats_are_not_retries():
    assert groups(run(call("fetch"), call("fetch"))) == []


def test_mixed_and_unknown_causes():
    (group,) = groups(
        run(
            call("fetch", status="error", reason="timeout"),
            call("fetch", status="error", reason="invalid_output_schema"),
            call("fetch"),
        )
    )
    assert group["cause"] == "mixed" and group["verdict"] == "unknown"


# Loops ------------------------------------------------------------------------------


def test_identical_calls_and_oscillation_are_found():
    trace = run(*(call("search", key="q1") for _ in range(4)))
    assert find_loops(trace)["repeated_identical_calls"] == [
        {"step": "search", "identical_calls": 4}
    ]
    trace = run(*(call(name) for _ in range(3) for name in ("open", "close")))
    assert find_loops(trace)["oscillation"] == {
        "pattern": ["open", "close"],
        "period": 2,
        "repeats": 3,
    }


def test_calls_without_input_keys_are_never_identical():
    assert find_loops(run(*(call("search") for _ in range(5))))["repeated_identical_calls"] == []


def test_replay_limits_finds_the_stop_point():
    trace = run(*(call("search", key="q1") for _ in range(4)))
    assert replay_limits(LoopLimits(max_identical_calls=3), trace)["reason"] == "identical_calls"
    assert replay_limits(LoopLimits(max_identical_calls=4), trace) == {"stopped": False}
    with pytest.raises(ValueError):
        replay_limits({"max_identical_calls": 3}, trace)


# Proposals and comparison ------------------------------------------------------------


def wasteful():
    return run(
        call("plan", status="error", reason="invalid_output_schema"),
        call("plan", status="error", reason="invalid_output_schema"),
        call("plan", status="error", reason="invalid_output_schema"),
        call("plan", status="error", reason="invalid_output_schema"),
    )


def healthy(retries=1):
    events = [call("plan", status="error", reason="invalid_output_schema") for _ in range(retries)]
    return run(*events, call("plan"))


def test_proposal_never_stops_a_completed_run():
    result = propose_limits(
        [
            {"trace": wasteful(), "completed": False},
            {"trace": healthy(2), "completed": True},
            {"trace": healthy(1), "completed": True},
        ]
    )
    assert result["status"] == "candidate"
    assert result["limits"]["max_retries_per_step"] == 2
    assert result["would_stop"] == [0]
    assert result["mode"] == "shadow"


def test_proposal_abstains_without_completed_runs_or_waste():
    assert propose_limits([{"trace": wasteful(), "completed": False}])["status"] == "abstained"
    assert propose_limits([])["status"] == "abstained"
    legit = run(call("fetch", status="error", reason="timeout"), call("fetch"))
    result = propose_limits([{"trace": legit, "completed": True}])
    assert result["status"] == "abstained" and "legitimate" in result["reasons"][0]
    with pytest.raises(ValueError):
        propose_limits([{"trace": legit, "completed": "yes"}])


def test_oscillation_limit_admits_completed_runs():
    looping = run(*(call(name) for _ in range(6) for name in ("open", "close")))
    finished = run(*(call(name) for _ in range(3) for name in ("open", "close")), call("submit"))
    result = propose_limits(
        [{"trace": looping, "completed": False}, {"trace": finished, "completed": True}]
    )
    assert result["status"] == "candidate"
    assert result["limits"]["oscillation_period"] == 2
    assert result["limits"]["oscillation_repeats"] == 4
    assert replay_limits(LoopLimits(**result["limits"]), finished) == {"stopped": False}
    assert result["would_stop"] == [0]


def test_candidates_are_shadow_policies_only():
    result = propose_limits(
        [{"trace": wasteful(), "completed": False}, {"trace": healthy(1), "completed": True}]
    )
    config = shadow_policy(result)
    assert config.mode == "shadow"
    with pytest.raises(ValueError):
        shadow_policy({"status": "abstained"})


def test_faster_but_prematurely_stopped_candidate_is_rejected():
    baseline = [{"runtime_ms": 9000, "completed": True} for _ in range(5)]
    faster_but_incomplete = [{"runtime_ms": 3000, "completed": index != 0} for index in range(5)]
    assert compare_runs(baseline, faster_but_incomplete)["verdict"] == "rejected_incomplete"
    faster = [{"runtime_ms": 7000, "completed": True} for _ in range(5)]
    assert compare_runs(baseline, faster)["verdict"] == "improved"
    assert compare_runs(baseline, baseline)["verdict"] == "no_gain"
    assert compare_runs([], faster)["verdict"] == "insufficient_evidence"
