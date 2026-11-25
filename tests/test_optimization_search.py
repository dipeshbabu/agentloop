"""Bounded multi-objective optimization search (issue #402)."""

from __future__ import annotations

import threading

import pytest

from agentloop.optimization_search import (
    Intervention,
    Objective,
    SearchBudget,
    SearchError,
    SearchSpec,
    run_search,
)

# Effects per intervention: runtime saved (ms), cost saved, quality change.
EFFECTS = {
    "reuse": (300, 0.02, 0.0),
    "parallel": (200, 0.0, 0.0),
    "small_model": (100, 0.05, -0.2),  # cheaper, but worse answers
    "cache_all": (400, 0.0, 0.0),
}


def evaluate_factory(effects=EFFECTS, interaction=0.0, held_out_penalty=0.0, calls=None):
    def evaluate(combo, case):
        if calls is not None:
            calls.append((combo, case))
        runtime, cost, quality = 1000.0, 0.10, 0.9
        for name in combo:
            saved, cost_saved, dq = effects[name]
            runtime -= saved
            cost -= cost_saved
            quality += dq
        if len(combo) > 1:
            runtime += interaction
        if case.startswith("h") and combo:
            quality -= held_out_penalty
        return {"quality": quality, "runtime_ms": runtime, "cost_usd": round(cost, 6)}

    return evaluate


def spec(**overrides):
    values = {
        "interventions": [
            Intervention("reuse"),
            Intervention("parallel"),
            Intervention("small_model"),
        ],
        "tuning_cases": ["t1", "t2"],
        "held_out_cases": ["h1", "h2"],
        "objectives": [Objective("runtime_ms", "min"), Objective("cost_usd", "min")],
        "budget": SearchBudget(max_attempts=100),
        "seed": 3,
    }
    values.update(overrides)
    return SearchSpec(**values)


def test_greedy_search_recommends_compatible_combination_for_review():
    report = run_search(spec(), evaluate_factory())
    assert report["decision"]["status"] == "recommend_for_review"
    assert report["selected"] == ["parallel", "reuse"]
    assert report["deployed"] is False
    # small_model is cheaper but loses quality, so it is never selected.
    small = next(c for c in report["candidates"] if c["interventions"] == ["small_model"])
    assert small["quality_ok"] is False and small["quality_regressions"]
    assert report["held_out"]["net_gain"]["runtime_ms"] == 500
    assert report["decision"]["interaction"] == 0


def test_all_attempts_and_the_baseline_are_retained():
    report = run_search(spec(), evaluate_factory())
    phases = [a["phase"] for a in report["attempts"]]
    assert phases.count("baseline") == 2
    assert phases.count("single") == 6
    assert phases.count("held_out") == 4
    assert report["overhead"]["attempts"] == len(report["attempts"])


def test_conflicting_interventions_are_never_combined():
    calls = []
    run_search(
        spec(
            interventions=[
                Intervention("reuse", conflicts_with={"cache_all"}),
                Intervention("cache_all"),
            ]
        ),  # fmt: skip
        evaluate_factory(calls=calls),
    )
    assert not any(set(combo) == {"reuse", "cache_all"} for combo, _ in calls)


def test_interactions_are_measured():
    report = run_search(spec(), evaluate_factory(interaction=150.0))
    assert report["decision"]["interaction"] == -150


def test_negative_interaction_can_stop_combination():
    report = run_search(spec(), evaluate_factory(interaction=500.0))
    assert report["selected"] == ["reuse"]


def test_held_out_regression_rejects():
    report = run_search(spec(), evaluate_factory(held_out_penalty=0.5))
    assert report["decision"]["status"] == "rejected"
    assert "held-out" in report["decision"]["reason"]


def test_attempt_budget_exhaustion_is_reported():
    report = run_search(spec(budget=SearchBudget(max_attempts=5)), evaluate_factory())
    assert report["stop_reason"] == "attempt_budget_exhausted"
    assert report["decision"]["status"] == "incomplete"
    assert len(report["attempts"]) == 5


def test_time_budget_uses_the_clock():
    ticks = iter(range(1000))
    report = run_search(
        spec(budget=SearchBudget(max_elapsed_s=4)), evaluate_factory(), clock=lambda: next(ticks)
    )
    assert report["stop_reason"] == "time_budget_exhausted"


def test_unknown_cost_stops_a_cost_budget():
    def evaluate(combo, case):
        return {"quality": 0.9, "runtime_ms": 900.0, "cost_usd": None}

    report = run_search(spec(budget=SearchBudget(max_cost_usd=1.0)), evaluate)
    assert report["stop_reason"] == "cost_unknown"
    assert report["attempts"][-1]["metrics"]["cost_usd"] is None


def test_cost_budget_exhaustion():
    report = run_search(spec(budget=SearchBudget(max_cost_usd=0.25)), evaluate_factory())
    assert report["stop_reason"] == "cost_budget_exhausted"
    assert report["overhead"]["measured_cost_usd"] >= 0.25


def test_unknown_objective_values_never_win():
    def evaluate(combo, case):
        return {"quality": 0.9, "runtime_ms": None if combo else 1000.0, "cost_usd": 0.1}

    report = run_search(spec(), evaluate)
    assert report["decision"]["status"] == "no_improvement"


def test_interrupted_attempt_is_retained_and_stops():
    def evaluate(combo, case):
        if combo == ("parallel",):
            raise KeyboardInterrupt
        return evaluate_factory()(combo, case)

    report = run_search(spec(seed=0), evaluate)
    assert report["stop_reason"] == "interrupted"
    assert report["attempts"][-1]["status"] == "interrupted"


def test_cancel_event_stops_between_attempts():
    cancel = threading.Event()

    def evaluate(combo, case):
        cancel.set()
        return evaluate_factory()(combo, case)

    report = run_search(spec(), evaluate, cancel=cancel)
    assert report["stop_reason"] == "cancelled"
    assert len(report["attempts"]) == 1


@pytest.mark.parametrize(
    "raw",
    [None, {"quality": "high"}, {"quality": float("nan")}, {"status": "weird", "quality": 1}],
)
def test_invalid_candidates_are_retained_and_excluded(raw):
    def evaluate(combo, case):
        return raw if combo == ("reuse",) else evaluate_factory()(combo, case)

    report = run_search(spec(), evaluate)
    bad = [a for a in report["attempts"] if a["interventions"] == ["reuse"]]
    assert all(a["status"] == "invalid" for a in bad)
    assert "reuse" not in report["selected"]


def test_failed_evaluations_are_retained():
    def evaluate(combo, case):
        if combo == ("parallel",):
            raise RuntimeError("tool crashed")
        return evaluate_factory()(combo, case)

    report = run_search(spec(), evaluate)
    failed = [a for a in report["attempts"] if a["status"] == "failed"]
    assert len(failed) == 2 and "RuntimeError" in failed[0]["reason"]
    assert report["selected"] == ["reuse"]


def test_seeded_search_is_deterministic():
    first = run_search(spec(), evaluate_factory())
    second = run_search(spec(), evaluate_factory())
    assert first["report_sha256"] == second["report_sha256"]
    other = run_search(spec(seed=99), evaluate_factory())
    assert other["selected"] == first["selected"]


def test_pareto_front_keeps_tradeoffs():
    report = run_search(spec(), evaluate_factory())
    assert ["parallel", "reuse"] in report["pareto"]
    assert ["small_model"] not in report["pareto"]  # fails the quality constraint


@pytest.mark.parametrize(
    "overrides",
    [
        {"held_out_cases": ["t1"]},
        {"case_digests": {"t1": "same", "h1": "same"}},
        {"interventions": []},
        {"interventions": [Intervention("a"), Intervention("a")]},
        {"interventions": [Intervention("a", conflicts_with={"ghost"})]},
        {"objectives": []},
        {"quality_floor": 2},
        {"tuning_cases": []},
    ],
)
def test_leakage_and_invalid_specifications_are_refused(overrides):
    with pytest.raises(SearchError):
        spec(**overrides)


def test_quality_is_a_constraint_not_an_objective():
    with pytest.raises(SearchError):
        Objective("quality")
    with pytest.raises(SearchError):
        Objective("runtime_ms", "sideways")


def test_budget_validation():
    for kwargs in ({"max_attempts": 0}, {"max_elapsed_s": 0}, {"max_cost_usd": -1}):
        with pytest.raises(SearchError):
            SearchBudget(**kwargs)
