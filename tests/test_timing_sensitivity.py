"""Counterfactual timing sensitivity and prediction checks (issue #397)."""

from __future__ import annotations

import pytest

from agentloop.events import AgentEvent
from agentloop.execution_ir import build_execution_ir
from agentloop.timing_sensitivity import compare_prediction, timing_sensitivity
from agentloop.tracer import AgentTrace


def ev(event_id, start, end, *, parent=None, deps=None, name=None, timed=True, metadata=None):
    meta = dict(metadata or {})
    if deps is not None:
        meta["depends_on"] = deps
    return AgentEvent(
        event_id=event_id,
        run_id="run",
        event_type="tool_call",
        name=name or event_id,
        started_at=f"2026-01-01T00:00:{start:06.3f}+00:00" if timed else None,
        ended_at=f"2026-01-01T00:00:{end:06.3f}+00:00" if timed else None,
        duration_ms=round((end - start) * 1000, 3),
        parent_id=parent,
        metadata=meta,
    )


def trace(*events):
    value = AgentTrace(name="t", run_id="run")
    for item in events:
        value.add_event(item)
    return value


def by_id(result):
    return {item["id"]: item for item in result["operations"]}


def fork_join():
    # plan(1s) -> {slow(4s), fast(1s)} -> answer(1s); runtime 6s.
    return trace(
        ev("plan", 0, 1, deps=[]),
        ev("slow", 1, 5, deps=["plan"]),
        ev("fast", 1, 2, deps=["plan"]),
        ev("answer", 5, 6, deps=["slow", "fast"]),
    )


def test_fork_join_critical_path_and_gains():
    result = timing_sensitivity(fork_join())
    assert result["status"] == "available"
    assert result["evidence"] == "simulated"
    assert result["observed_runtime_ms"] == result["modelled_runtime_ms"] == 6000
    assert result["critical_path"] == ["plan", "slow", "answer"]
    ops = by_id(result)
    # Removing the slow branch leaves the fast one: 4s - 1s = 3s saved.
    assert ops["slow"]["gain_if_removed_ms"] == 3000
    assert ops["slow"]["gain_if_scaled_ms"] == 2000
    assert ops["plan"]["gain_if_removed_ms"] == 1000
    assert result["operations"][0]["id"] == "slow"


def test_noncritical_operation_is_a_negative_control():
    ops = by_id(timing_sensitivity(fork_join()))
    assert ops["fast"]["on_critical_path"] is False
    assert ops["fast"]["slack_ms"] == 3000
    assert ops["fast"]["gain_if_removed_ms"] == 0
    assert ops["fast"]["gain_if_scaled_ms"] == 0


def test_critical_path_migrates():
    ops = by_id(timing_sensitivity(fork_join()))
    assert ops["slow"]["critical_path_after_removal"] == ["plan", "fast", "answer"]
    assert ops["slow"]["bottleneck_after_removal"] in {"plan", "fast", "answer"}


def test_overlapping_independent_spans():
    # Two concurrent root calls; only the longer one bounds the runtime.
    result = timing_sensitivity(trace(ev("a", 0, 3, deps=[]), ev("b", 0.5, 2, deps=[])))
    ops = by_id(result)
    assert result["modelled_runtime_ms"] == 3000
    assert ops["b"]["gain_if_removed_ms"] == 0
    assert ops["a"]["gain_if_removed_ms"] == 1000  # b still ends at 2s


def test_nested_parent_is_composite_and_children_drive_it():
    result = timing_sensitivity(
        trace(
            ev("agent", 0, 5),
            ev("step1", 0.5, 2.5, parent="agent", deps=[]),
            ev("step2", 2.5, 4.5, parent="agent", deps=["step1"]),
        )
    )
    ops = by_id(result)
    assert ops["agent"]["composite"] is True and "gain_if_removed_ms" not in ops["agent"]
    # Removing step1 lets step2 start at 0.5s; the parent keeps its 0.5s tail.
    assert ops["step1"]["gain_if_removed_ms"] == 2000


def test_undeclared_siblings_keep_their_offsets():
    # Without declared dependencies step2 keeps its observed start, so a
    # faster step1 changes nothing. This is the conservative reading.
    ops = by_id(
        timing_sensitivity(
            trace(
                ev("agent", 0, 5),
                ev("step1", 0.5, 2.5, parent="agent"),
                ev("step2", 2.5, 4.5, parent="agent"),
            )
        )
    )
    assert ops["step1"]["gain_if_removed_ms"] == 0


def test_inferred_root_sequence_propagates():
    ops = by_id(timing_sensitivity(trace(ev("a", 0, 2), ev("b", 2, 3))))
    assert ops["a"]["gain_if_removed_ms"] == 2000


@pytest.mark.parametrize(
    ("events", "fragment"),
    [
        ((ev("p", 0, 1), ev("c", 0.5, 3, parent="p")), "outside its parent"),
        ((ev("a", 0, 2, deps=[]), ev("b", 1, 3, deps=["a"])), "before a ends"),
        ((ev("a", 0, 1), ev("b", 0, 0, timed=False)), "timing is missing"),
        ((ev("a", 0, 1, deps=["b"]), ev("b", 1, 2, deps=["a"])), "cycle"),
        ((ev("a", 0, 1, deps=["ghost"]),), "not in this trace"),
    ],
)
def test_abstains_on_invalid_or_incomplete_timing(events, fragment):
    result = timing_sensitivity(trace(*events))
    assert result["status"] == "abstained"
    assert any(fragment in reason for reason in result["reasons"]), result["reasons"]


def test_timestamps_that_disagree_with_duration_abstain():
    # Older adapters stamped both times "now" and kept the real duration
    # separately; such clocks cannot be simulated.
    event = ev("a", 0, 1)
    event.duration_ms = 5000.0
    result = timing_sensitivity(trace(event, ev("b", 1, 2)))
    assert result["status"] == "abstained"
    assert any("timing is missing" in reason for reason in result["reasons"])


def test_empty_trace_and_bad_scale():
    assert timing_sensitivity(trace())["status"] == "abstained"
    for scale in (1, -0.1, float("nan"), True):
        with pytest.raises(ValueError):
            timing_sensitivity(fork_join(), scale=scale)


def test_accepts_a_prebuilt_ir_and_is_deterministic():
    ir = build_execution_ir(fork_join())
    assert timing_sensitivity(ir) == timing_sensitivity(fork_join())


# Prediction versus measurement -------------------------------------------------


def test_prediction_inside_measured_interval():
    result = compare_prediction(
        3000, [6000, 6100, 5900, 6050, 5950], [3000, 3100, 2900, 3050, 2950]
    )
    assert result["status"] == "available"
    assert result["measured_gain_ms"] == 3000
    assert result["prediction_within_interval"] is True
    assert result["measured_effect_detected"] is True
    low, high = result["prediction_error_interval_ms"]
    assert low <= 0 <= high
    assert result["prediction_evidence"] == "simulated"


def test_negative_control_measures_no_effect():
    # A non-critical change predicted to save nothing; runs overlap.
    runs = [6000, 6100, 5900, 6050, 5950]
    result = compare_prediction(0, runs, list(reversed(runs)))
    assert result["measured_effect_detected"] is False
    assert result["prediction_within_interval"] is True


def test_overconfident_prediction_is_flagged():
    result = compare_prediction(3000, [6000, 6010, 5990], [5000, 5010, 4990])
    assert result["prediction_within_interval"] is False
    assert result["prediction_error_ms"] == 2000


def test_comparison_abstains_and_drops_invalid_runs():
    result = compare_prediction(1, [1.0, float("nan")], [1.0, -2, None])
    assert result["status"] == "abstained"
    assert result["dropped_runs"] == 3
    with pytest.raises(ValueError):
        compare_prediction(float("inf"), [1, 2], [1, 2])
    with pytest.raises(ValueError):
        compare_prediction(1, [1, 2], [1, 2], resamples=10)


def test_comparison_is_reproducible():
    args = (100, [10, 20, 30, 40], [5, 6, 7, 8])
    assert compare_prediction(*args) == compare_prediction(*args)
