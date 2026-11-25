"""Bounded, seeded multi-objective search over candidate interventions (issue #402).

The search decides which combination of interventions to recommend for review.
It never applies or deploys one. You supply the interventions, the cases, and an
``evaluate`` callback that runs one combination on one case and reports what
happened; the search owns ordering, budgets and bookkeeping.

```python
spec = SearchSpec(
    interventions=[Intervention("reuse", conflicts_with={"no_cache"}), Intervention("parallel")],
    tuning_cases=["t1", "t2"],
    held_out_cases=["h1", "h2"],
    objectives=[Objective("runtime_ms", "min"), Objective("cost_usd", "min")],
    quality_floor=0.0,  # allowed quality drop versus the baseline, per case
    budget=SearchBudget(max_attempts=60, max_elapsed_s=600),
    seed=7,
)
report = run_search(spec, evaluate)
```

``evaluate(interventions, case_id)`` returns a mapping with ``quality``
(a number, required), the objective metrics (numbers, or ``None`` when
unknown), and optionally ``"status": "error"``. An exception marks the attempt
``failed``; ``KeyboardInterrupt`` or the ``cancel`` event marks it
``interrupted`` and ends the search. Every attempt is kept.

Order of work:

1. The baseline (no interventions) on every tuning case.
2. Each single intervention on every tuning case.
3. Greedy combination: starting from the best single that keeps quality, add
   the next compatible intervention while it improves the primary objective.
   Combinations are only tried when no declared conflict exists; each reports
   its interaction (its gain minus the sum of its parts' gains).
4. Once, at the end: the selected combination and the baseline on the
   held-out cases, which are never seen during tuning.

Budgets stop the search early and say why. If a cost budget is set and an
attempt reports unknown cost, the budget can't be checked, so the search stops
rather than guess.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "agentloop.optimization_search/v1"


class SearchError(ValueError):
    """The search specification is invalid; nothing was evaluated."""


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise SearchError(f"{label} must be nonempty text of at most 128 characters")
    return value


@dataclass(frozen=True)
class Intervention:
    name: str
    conflicts_with: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _name(self.name, "intervention name")
        object.__setattr__(self, "conflicts_with", frozenset(self.conflicts_with))


@dataclass(frozen=True)
class Objective:
    metric: str
    direction: str = "min"

    def __post_init__(self) -> None:
        _name(self.metric, "objective metric")
        if self.metric == "quality":
            raise SearchError("quality is a constraint, not an objective")
        if self.direction not in {"min", "max"}:
            raise SearchError("objective direction must be 'min' or 'max'")


@dataclass(frozen=True)
class SearchBudget:
    max_attempts: int = 100
    max_elapsed_s: float = 3600.0
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100_000:
            raise SearchError("max_attempts must be an integer from 1 to 100000")
        if not isinstance(self.max_elapsed_s, int | float) or not 0 < self.max_elapsed_s <= 86_400:
            raise SearchError("max_elapsed_s must be positive and at most a day")
        if self.max_cost_usd is not None and (
            not isinstance(self.max_cost_usd, int | float) or not self.max_cost_usd > 0
        ):
            raise SearchError("max_cost_usd must be positive when set")


@dataclass(frozen=True)
class SearchSpec:
    interventions: Sequence[Intervention]
    tuning_cases: Sequence[str]
    held_out_cases: Sequence[str]
    objectives: Sequence[Objective]
    quality_floor: float = 0.0
    budget: SearchBudget = field(default_factory=SearchBudget)
    seed: int = 0
    max_combination_size: int = 3
    case_digests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = [item.name for item in self.interventions]
        if not names or len(names) != len(set(names)) or len(names) > 64:
            raise SearchError("interventions must be 1 to 64 uniquely named items")
        for item in self.interventions:
            unknown = item.conflicts_with - set(names)
            if unknown:
                raise SearchError(f"{item.name} conflicts with unknown {sorted(unknown)}")
        for label, cases in (("tuning", self.tuning_cases), ("held-out", self.held_out_cases)):
            if not cases or len(set(cases)) != len(cases):
                raise SearchError(f"{label} cases must be a nonempty list of unique ids")
            for case in cases:
                _name(case, f"{label} case id")
        shared = set(self.tuning_cases) & set(self.held_out_cases)
        if shared:
            raise SearchError(f"cases appear in both tuning and held-out sets: {sorted(shared)}")
        digests = dict(self.case_digests)
        tuning = {digests[c] for c in self.tuning_cases if c in digests}
        held = {digests[c] for c in self.held_out_cases if c in digests}
        if tuning & held:
            raise SearchError("a held-out case has the same content digest as a tuning case")
        if not self.objectives or len({o.metric for o in self.objectives}) != len(self.objectives):
            raise SearchError("objectives must be a nonempty list of distinct metrics")
        if not isinstance(self.quality_floor, int | float) or not 0 <= self.quality_floor <= 1:
            raise SearchError("quality_floor is the allowed drop, from 0 to 1")
        if type(self.seed) is not int:
            raise SearchError("seed must be an integer")
        if type(self.max_combination_size) is not int or not 1 <= self.max_combination_size <= 8:
            raise SearchError("max_combination_size must be from 1 to 8")

    def frozen(self) -> dict[str, Any]:
        return {
            "interventions": [
                {"name": i.name, "conflicts_with": sorted(i.conflicts_with)}
                for i in self.interventions
            ],
            "tuning_cases": list(self.tuning_cases),
            "held_out_cases": list(self.held_out_cases),
            "objectives": [{"metric": o.metric, "direction": o.direction} for o in self.objectives],
            "quality_floor": self.quality_floor,
            "budget": {
                "max_attempts": self.budget.max_attempts,
                "max_elapsed_s": self.budget.max_elapsed_s,
                "max_cost_usd": self.budget.max_cost_usd,
            },
            "seed": self.seed,
            "max_combination_size": self.max_combination_size,
        }


class _StopSearchError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Runner:
    def __init__(self, spec, evaluate, cancel, clock):
        self.spec, self.evaluate, self.cancel, self.clock = spec, evaluate, cancel, clock
        self.started = clock()
        self.attempts: list[dict[str, Any]] = []
        self.cost = 0.0

    def _check_budget(self) -> None:
        budget = self.spec.budget
        if self.cancel is not None and self.cancel.is_set():
            raise _StopSearchError("cancelled")
        if len(self.attempts) >= budget.max_attempts:
            raise _StopSearchError("attempt_budget_exhausted")
        if self.clock() - self.started >= budget.max_elapsed_s:
            raise _StopSearchError("time_budget_exhausted")
        if budget.max_cost_usd is not None and self.cost >= budget.max_cost_usd:
            raise _StopSearchError("cost_budget_exhausted")

    def attempt(self, combo: tuple[str, ...], case: str, phase: str) -> dict[str, Any]:
        self._check_budget()
        record: dict[str, Any] = {
            "index": len(self.attempts),
            "phase": phase,
            "interventions": list(combo),
            "case": case,
        }
        start = self.clock()
        try:
            raw = self.evaluate(combo, case)
        except KeyboardInterrupt:
            record.update(status="interrupted", reason="interrupted by the caller")
            self.attempts.append(record)
            raise _StopSearchError("interrupted") from None
        except Exception as error:  # noqa: BLE001 - every failure is retained as evidence
            record.update(status="failed", reason=f"{type(error).__name__}: {error}"[:500])
            self.attempts.append(record)
            return record
        finally:
            record["evaluation_s"] = round(self.clock() - start, 6)
        record.update(_validated(raw, self.spec))
        self.attempts.append(record)
        cost = record.get("metrics", {}).get("cost_usd")
        if self.spec.budget.max_cost_usd is not None:
            if cost is None and record["status"] == "ok":
                raise _StopSearchError("cost_unknown")
            self.cost += cost or 0.0
        if self.cancel is not None and self.cancel.is_set():
            raise _StopSearchError("cancelled")
        return record


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _validated(raw: Any, spec: SearchSpec) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"status": "invalid", "reason": "evaluate must return a mapping"}
    if raw.get("status", "ok") not in {"ok", "error"}:
        return {"status": "invalid", "reason": "status must be 'ok' or 'error'"}
    if raw.get("status") == "error":
        return {"status": "error", "reason": str(raw.get("reason", "reported an error"))[:500]}
    quality = _number(raw.get("quality"))
    if quality is None:
        return {"status": "invalid", "reason": "quality must be a finite number"}
    metrics = {"quality": quality}
    for objective in spec.objectives:
        metrics[objective.metric] = _number(raw.get(objective.metric))
    if "cost_usd" in raw and "cost_usd" not in metrics:
        metrics["cost_usd"] = _number(raw.get("cost_usd"))
    return {"status": "ok", "metrics": metrics}


def run_search(
    spec: SearchSpec,
    evaluate: Callable[[tuple[str, ...], str], Mapping[str, Any]],
    *,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the search. Returns a report for review; never applies anything."""
    if not isinstance(spec, SearchSpec):
        raise SearchError("spec must be a SearchSpec")
    if not callable(evaluate):
        raise SearchError("evaluate must be callable")
    runner = _Runner(spec, evaluate, cancel, clock)
    rng = random.Random(spec.seed)  # nosec B311 - seeded tie order, not security
    primary = spec.objectives[0]
    conflicts = {i.name: i.conflicts_with for i in spec.interventions}
    results: dict[tuple[str, ...], dict[str, Any]] = {}
    stop_reason = None
    selected: tuple[str, ...] = ()
    try:
        results[()] = _measure(runner, (), spec.tuning_cases, "baseline")
        singles = [(i.name,) for i in spec.interventions]
        rng.shuffle(singles)  # seeded: deterministic tie order, not a quality signal
        for combo in singles:
            results[combo] = _measure(runner, combo, spec.tuning_cases, "single")
        selected = _greedy(runner, spec, results, conflicts, primary)
    except _StopSearchError as stop:
        stop_reason = stop.reason

    summaries = {combo: _summarize(row, results.get(()), spec) for combo, row in results.items()}
    pareto = _pareto(summaries, spec)
    held_out = None
    if stop_reason is None and selected:
        try:
            held_out = {
                "baseline": _measure(runner, (), spec.held_out_cases, "held_out"),
                "selected": _measure(runner, selected, spec.held_out_cases, "held_out"),
            }
        except _StopSearchError as stop:
            stop_reason = stop.reason
    decision = _decision(selected, summaries, held_out, spec, stop_reason)
    elapsed = clock() - runner.started
    report = {
        "schema": SCHEMA,
        "specification": spec.frozen(),
        "stop_reason": stop_reason,
        "attempts": runner.attempts,
        "candidates": [
            {"interventions": list(combo), **summary}
            for combo, summary in sorted(
                summaries.items(), key=lambda item: (len(item[0]), item[0])
            )
        ],
        "pareto": [list(combo) for combo in pareto],
        "selected": list(selected),
        "held_out": _held_out_summary(held_out, spec) if held_out else None,
        "decision": decision,
        "overhead": {
            "attempts": len(runner.attempts),
            "elapsed_s": round(elapsed, 6),
            "evaluation_s": round(sum(a.get("evaluation_s", 0.0) for a in runner.attempts), 6),
            "measured_cost_usd": round(runner.cost, 6) if spec.budget.max_cost_usd else None,
        },
        "deployed": False,
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(_stable(report), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return report


def _stable(report: dict[str, Any]) -> dict[str, Any]:
    """The report without wall-clock fields, for a reproducible digest."""
    copy = json.loads(json.dumps(report, default=str))
    copy.pop("overhead", None)
    for attempt in copy["attempts"]:
        attempt.pop("evaluation_s", None)
    return copy


def _measure(runner, combo, cases, phase) -> dict[str, Any]:
    return {case: runner.attempt(combo, case, phase) for case in cases}


def _greedy(runner, spec, results, conflicts, primary) -> tuple[str, ...]:
    baseline = _summarize(results[()], None, spec)
    acceptable = [
        combo
        for combo in results
        if combo and _summarize(results[combo], results[()], spec)["quality_ok"]
    ]
    scored = [
        (combo, _summarize(results[combo], results[()], spec)["means"].get(primary.metric))
        for combo in acceptable
    ]
    scored = [(combo, value) for combo, value in scored if value is not None]
    base_value = baseline["means"].get(primary.metric)
    if not scored or base_value is None:
        return ()
    better = lambda a, b: a < b if primary.direction == "min" else a > b  # noqa: E731
    scored.sort(key=lambda item: item[1] if primary.direction == "min" else -item[1])
    current, current_value = scored[0]
    if not better(current_value, base_value):
        return ()
    for candidate, _ in scored[1:]:
        if len(current) >= spec.max_combination_size:
            break
        name = candidate[0]
        if any(name in conflicts[m] or m in conflicts[name] for m in current):
            continue
        combo = tuple(sorted((*current, name)))
        if combo in results:
            continue
        results[combo] = _measure(runner, combo, spec.tuning_cases, "combination")
        summary = _summarize(results[combo], results[()], spec)
        value = summary["means"].get(primary.metric)
        if summary["quality_ok"] and value is not None and better(value, current_value):
            current, current_value = combo, value
    return current


def _summarize(rows, baseline_rows, spec) -> dict[str, Any]:
    ok = {case: row for case, row in rows.items() if row["status"] == "ok"}
    complete = len(ok) == len(rows)
    means = {}
    for metric in ["quality", *(o.metric for o in spec.objectives)]:
        values = [row["metrics"].get(metric) for row in ok.values()]
        known = [v for v in values if v is not None]
        means[metric] = (
            round(statistics.fmean(known), 6)
            if known and len(known) == len(values) and complete
            else None
        )
    quality_ok = complete
    regressions = []
    if baseline_rows is not None:
        for case, row in rows.items():
            base = baseline_rows.get(case)
            if row["status"] != "ok" or base is None or base["status"] != "ok":
                quality_ok = False
                continue
            drop = base["metrics"]["quality"] - row["metrics"]["quality"]
            if drop > spec.quality_floor + 1e-12:
                quality_ok = False
                regressions.append({"case": case, "quality_drop": round(drop, 6)})
    return {
        "complete": complete,
        "failed_cases": sorted(case for case, row in rows.items() if row["status"] != "ok"),
        "means": means,
        "quality_ok": quality_ok,
        "quality_regressions": regressions,
    }


def _pareto(summaries, spec) -> list[tuple[str, ...]]:
    eligible = {
        combo: summary
        for combo, summary in summaries.items()
        if summary["quality_ok"] or combo == ()
        if all(summary["means"].get(o.metric) is not None for o in spec.objectives)
    }

    def dominates(a, b) -> bool:
        no_worse, better = True, False
        for objective in spec.objectives:
            x, y = a["means"][objective.metric], b["means"][objective.metric]
            if objective.direction == "max":
                x, y = -x, -y
            if x > y:
                no_worse = False
            if x < y:
                better = True
        return no_worse and better

    front = [
        combo
        for combo, summary in eligible.items()
        if not any(dominates(other, summary) for key, other in eligible.items() if key != combo)
    ]
    return sorted(front, key=lambda combo: (len(combo), combo))


def _held_out_summary(held_out, spec) -> dict[str, Any]:
    base = _summarize(held_out["baseline"], None, spec)
    selected = _summarize(held_out["selected"], held_out["baseline"], spec)
    gains = {}
    for objective in spec.objectives:
        b, s = base["means"].get(objective.metric), selected["means"].get(objective.metric)
        if b is None or s is None:
            gains[objective.metric] = None
        else:
            gains[objective.metric] = round((b - s) if objective.direction == "min" else (s - b), 6)
    return {"baseline": base, "selected": selected, "net_gain": gains}


def _decision(selected, summaries, held_out, spec, stop_reason) -> dict[str, Any]:
    if stop_reason is not None:
        return {
            "status": "incomplete",
            "reason": f"the search stopped early: {stop_reason}",
            "recommendation": None,
        }
    if not selected:
        return {
            "status": "no_improvement",
            "reason": "no intervention improved the primary objective without a quality regression",
            "recommendation": None,
        }
    held = _held_out_summary(held_out, spec)
    primary = spec.objectives[0].metric
    if not held["selected"]["quality_ok"]:
        return {
            "status": "rejected",
            "reason": "the selected combination regressed quality on held-out cases",
            "recommendation": None,
        }
    gain = held["net_gain"].get(primary)
    if gain is None or gain <= 0:
        return {
            "status": "rejected",
            "reason": "the selected combination did not improve the primary objective on held-out cases",
            "recommendation": None,
        }
    parts = [(name,) for name in selected]
    interaction = None
    if len(selected) > 1 and all(p in summaries for p in parts):
        base = summaries[()]["means"].get(primary)
        whole = summaries[tuple(selected)]["means"].get(primary)
        singles = [summaries[p]["means"].get(primary) for p in parts]
        if base is not None and whole is not None and None not in singles:
            sign = 1 if spec.objectives[0].direction == "min" else -1
            combined_gain = sign * (base - whole)
            sum_parts = sum(sign * (base - s) for s in singles)
            interaction = round(combined_gain - sum_parts, 6)
    return {
        "status": "recommend_for_review",
        "reason": "improved the primary objective on held-out cases without a quality regression",
        "recommendation": list(selected),
        "interaction": interaction,
    }


__all__ = [
    "Intervention",
    "Objective",
    "SCHEMA",
    "SearchBudget",
    "SearchError",
    "SearchSpec",
    "run_search",
]
