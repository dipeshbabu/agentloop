"""Counterfactual timing sensitivity over the execution IR (issue #397).

``timing_sensitivity`` asks: if one operation had been faster, how much sooner
would this recorded run have finished, holding everything else as observed?
The answer is a **simulation**: a hypothesis about this run's structure, not a
measured causal effect. ``compare_prediction`` sets a prediction against
measured baseline and candidate runtimes, with a bootstrap interval, so the
two are never confused.

Model
-----
Each operation has a start and an end. Constraints, all with observed weights:

* A leaf operation ends ``duration`` after it starts. That duration is what an
  intervention changes.
* A parent starts no later than its children's observed lead (``child start -
  parent start``) allows, and ends no earlier than each child's end plus the
  observed tail (``parent end - child end``).
* A declared ``data`` or ``state`` edge, or an inferred root ``sequence`` edge,
  starts its target no earlier than the source's end plus the observed gap.
* An operation with none of those keeps its observed start offset from its
  parent (or from the start of the trace).

Gaps, leads and tails are held fixed: queueing and scheduling delay do not
shrink, and contention does not grow. The simulation abstains when timing is
missing, clocks are inconsistent (a child outside its parent, overlapping
dependent operations), the IR has unsupported evidence, or the model cannot
reproduce the observed runtime.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from agentloop.execution_ir import build_execution_ir

SCHEMA = "agentloop.timing_sensitivity/v1"
CLOCK_TOLERANCE_MS = 1.0
REPRODUCTION_TOLERANCE = 0.01  # of the observed runtime, plus CLOCK_TOLERANCE_MS
ASSUMPTIONS = (
    "Simulated on this run's recorded structure; not a measured effect.",
    "Gaps between dependent operations (queueing, scheduling) are held fixed.",
    "Concurrency is unlimited at the observed level; contention could reduce gains.",
    "Operations with no declared or inferred predecessor keep their observed start offset.",
    "Parents are composite: change their children, not the parent.",
)


def _abstain(reasons: list[str]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "abstained",
        "reasons": reasons,
        "evidence": "simulated",
        "assumptions": list(ASSUMPTIONS),
    }


def timing_sensitivity(source: Any, *, scale: float = 0.5) -> dict[str, Any]:
    """Simulate removing, or scaling by ``scale``, each leaf operation's duration.

    ``source`` is a trace or an execution IR document. ``scale`` must be in [0, 1).
    """
    if not isinstance(scale, int | float) or isinstance(scale, bool):
        raise ValueError("scale must be a number")
    if not 0 <= scale < 1 or not math.isfinite(scale):
        raise ValueError("scale must be at least 0 and below 1")
    ir = source if isinstance(source, Mapping) else build_execution_ir(source)
    operations = list(ir.get("operations", []))
    if not operations:
        return _abstain(["the trace has no operations"])

    reasons: list[str] = []
    for item in ir.get("unsupported", []):
        reasons.append(f"{item['operation']}: {item['reason']}")
    timing: dict[str, tuple[float, float]] = {}
    for operation in operations:
        value = operation["timing"]["value"]
        if operation["timing"]["basis"] != "observed" or value is None:
            reasons.append(f"{operation['id']}: timing is missing or inconsistent")
            continue
        timing[operation["id"]] = (float(value["start_ms"]), float(value["end_ms"]))
    if reasons:
        return _abstain(reasons)

    by_id = {operation["id"]: operation for operation in operations}
    children: dict[str, list[str]] = {}
    sources: dict[str, list[str]] = {}
    for edge in ir.get("edges", []):
        source_id, target = edge["source"], edge["target"]
        if edge["kind"] == "contains":
            children.setdefault(source_id, []).append(target)
            continue
        overlap = timing[source_id][1] - timing[target][0]
        if overlap > CLOCK_TOLERANCE_MS:
            reasons.append(
                f"{target} starts {round(overlap, 3)} ms before {source_id} ends, "
                f"despite a {edge['kind']} edge"
            )
            continue
        if source_id not in sources.setdefault(target, []):
            sources[target].append(source_id)
    # The wait before a target is measured from its last predecessor to finish;
    # earlier predecessors were not what it waited for.
    predecessors: dict[str, list[tuple[str, float]]] = {}
    for target, ids in sources.items():
        binding = max(timing[source_id][1] for source_id in ids)
        gap = max(0.0, timing[target][0] - binding)
        predecessors[target] = [(source_id, gap) for source_id in ids]
    for parent, kids in children.items():
        start, end = timing[parent]
        for kid in kids:
            kid_start, kid_end = timing[kid]
            if kid_start < start - CLOCK_TOLERANCE_MS or kid_end > end + CLOCK_TOLERANCE_MS:
                reasons.append(f"{kid} runs outside its parent {parent}'s interval")
    if reasons:
        return _abstain(reasons)

    origin = min(start for start, _ in timing.values())
    observed = max(end for _, end in timing.values()) - origin
    leaves = [operation["id"] for operation in operations if operation["id"] not in children]
    durations = {op_id: timing[op_id][1] - timing[op_id][0] for op_id in leaves}

    model = _Model(by_id, timing, children, predecessors, origin)
    try:
        baseline = model.run(durations)
    except _CycleError as exc:
        return _abstain([str(exc)])
    if abs(baseline.makespan - observed) > CLOCK_TOLERANCE_MS + REPRODUCTION_TOLERANCE * observed:
        return _abstain(
            [
                f"the model gives {round(baseline.makespan, 3)} ms but the trace ran "
                f"{round(observed, 3)} ms; its structure is not captured well enough to simulate"
            ]
        )

    critical = set(baseline.critical_path)
    results = []
    for op_id in [operation["id"] for operation in operations]:
        entry: dict[str, Any] = {
            "id": op_id,
            "name": by_id[op_id]["name"],
            "on_critical_path": op_id in critical,
            "slack_ms": round(baseline.slack[op_id], 3),
        }
        if op_id in children:
            entry["composite"] = True
            results.append(entry)
            continue
        removed = model.run({**durations, op_id: 0.0})
        scaled = model.run({**durations, op_id: durations[op_id] * scale})
        entry.update(
            {
                "composite": False,
                "duration_ms": round(durations[op_id], 3),
                "gain_if_removed_ms": round(baseline.makespan - removed.makespan, 3),
                "gain_if_scaled_ms": round(baseline.makespan - scaled.makespan, 3),
                "critical_path_after_removal": removed.critical_path,
                "bottleneck_after_removal": _bottleneck(removed, durations, op_id),
            }
        )
        results.append(entry)
    results.sort(key=lambda item: (-item.get("gain_if_removed_ms", -1.0), item["id"]))
    return {
        "schema": SCHEMA,
        "status": "available",
        "evidence": "simulated",
        "scale": scale,
        "observed_runtime_ms": round(observed, 3),
        "modelled_runtime_ms": round(baseline.makespan, 3),
        "critical_path": baseline.critical_path,
        "operations": results,
        "assumptions": list(ASSUMPTIONS),
    }


def _bottleneck(result: _Result, durations: Mapping[str, float], removed: str) -> str | None:
    """The longest leaf still on the critical path once ``removed`` costs nothing."""
    remaining = [op_id for op_id in result.critical_path if op_id in durations and op_id != removed]
    if not remaining:
        return None
    return max(remaining, key=lambda op_id: (durations[op_id], op_id))


class _CycleError(ValueError):
    pass


class _Result:
    def __init__(self, makespan: float, critical_path: list[str], slack: dict[str, float]):
        self.makespan = makespan
        self.critical_path = critical_path
        self.slack = slack


class _Model:
    """Longest-path model over start and end events of each operation."""

    def __init__(self, by_id, timing, children, predecessors, origin):
        self._ids = list(by_id)
        self._edges: list[tuple[str, str, float, str | None]] = []
        parent_of = {kid: parent for parent, kids in children.items() for kid in kids}
        for op_id in self._ids:
            start, end = timing[op_id]
            constrained = op_id in predecessors
            if not constrained:
                anchor = parent_of.get(op_id)
                if anchor is None:
                    self._edges.append(("origin", f"S:{op_id}", start - origin, None))
                else:
                    self._edges.append(
                        (f"S:{anchor}", f"S:{op_id}", start - timing[anchor][0], None)
                    )
            for source_id, gap in predecessors.get(op_id, []):
                self._edges.append((f"E:{source_id}", f"S:{op_id}", gap, None))
            if op_id in parent_of and constrained:
                self._edges.append((f"S:{parent_of[op_id]}", f"S:{op_id}", 0.0, None))
            if op_id in children:
                # A parent ends after each child, plus that child's observed tail.
                for kid in children[op_id]:
                    self._edges.append((f"E:{kid}", f"E:{op_id}", end - timing[kid][1], None))
                self._edges.append((f"S:{op_id}", f"E:{op_id}", 0.0, None))
            else:
                self._edges.append((f"S:{op_id}", f"E:{op_id}", 0.0, op_id))
        self._order = self._topological()

    def _topological(self) -> list[str]:
        nodes = {"origin"} | {f"{p}:{op_id}" for op_id in self._ids for p in ("S", "E")}
        indegree = dict.fromkeys(nodes, 0)
        self._out: dict[str, list[tuple[str, float, str | None]]] = {node: [] for node in nodes}
        for source, target, weight, leaf in self._edges:
            self._out[source].append((target, weight, leaf))
            indegree[target] += 1
        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        order = []
        while ready:
            node = ready.pop()
            order.append(node)
            for target, _, _ in self._out[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if len(order) != len(nodes):
            raise _CycleError("operations form a timing cycle")
        return order

    def run(self, durations: Mapping[str, float]) -> _Result:
        earliest = dict.fromkeys(self._order, 0.0)
        best_parent: dict[str, str | None] = dict.fromkeys(self._order)
        for node in self._order:
            for target, weight, leaf in self._out[node]:
                value = earliest[node] + (durations[leaf] if leaf is not None else weight)
                if value > earliest[target] + 1e-9:
                    earliest[target] = value
                    best_parent[target] = node
        end_nodes = [node for node in self._order if node.startswith("E:")]
        makespan = max(earliest[node] for node in end_nodes)
        latest = dict.fromkeys(self._order, makespan)
        for node in reversed(self._order):
            for target, weight, leaf in self._out[node]:
                value = latest[target] - (durations[leaf] if leaf is not None else weight)
                latest[node] = min(latest[node], value)
        slack = {
            op_id: max(0.0, latest[f"S:{op_id}"] - earliest[f"S:{op_id}"]) for op_id in self._ids
        }
        last = max(end_nodes, key=lambda node: (earliest[node], node))
        path: list[str] = []
        node: str | None = last
        while node is not None and node != "origin":
            op_id = node[2:]
            if op_id not in path:
                path.append(op_id)
            node = best_parent[node]
        path.reverse()
        return _Result(makespan, path, slack)


def compare_prediction(
    predicted_gain_ms: float,
    baseline_runtimes_ms: Sequence[float],
    candidate_runtimes_ms: Sequence[float],
    *,
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Set a simulated gain against a measured one from separate runs.

    The measured gain is the difference of medians (baseline minus candidate).
    Its interval comes from resampling each group independently with a fixed
    seed, so the result is reproducible. Abstains with fewer than two valid
    runs in either group; non-finite or negative runtimes are dropped and
    counted.
    """
    if not isinstance(predicted_gain_ms, int | float) or not math.isfinite(predicted_gain_ms):
        raise ValueError("predicted_gain_ms must be a finite number")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    if resamples < 100:
        raise ValueError("resamples must be at least 100")
    baseline, dropped_b = _clean(baseline_runtimes_ms)
    candidate, dropped_c = _clean(candidate_runtimes_ms)
    result: dict[str, Any] = {
        "predicted_gain_ms": round(float(predicted_gain_ms), 3),
        "prediction_evidence": "simulated",
        "measured_evidence": "paired_runs_unmatched",
        "baseline_runs": len(baseline),
        "candidate_runs": len(candidate),
        "dropped_runs": dropped_b + dropped_c,
    }
    if len(baseline) < 2 or len(candidate) < 2:
        return {
            **result,
            "status": "abstained",
            "reasons": ["need at least two valid runs in each group"],
        }
    measured = statistics.median(baseline) - statistics.median(candidate)
    rng = random.Random(seed)
    draws = sorted(
        statistics.median(rng.choices(baseline, k=len(baseline)))
        - statistics.median(rng.choices(candidate, k=len(candidate)))
        for _ in range(resamples)
    )
    tail = (1 - confidence) / 2
    low = draws[int(math.floor(tail * (resamples - 1)))]
    high = draws[int(math.ceil((1 - tail) * (resamples - 1)))]
    error = float(predicted_gain_ms) - measured
    return {
        **result,
        "status": "available",
        "measured_gain_ms": round(measured, 3),
        "measured_interval_ms": [round(low, 3), round(high, 3)],
        "confidence": confidence,
        "prediction_error_ms": round(error, 3),
        "prediction_error_interval_ms": [
            round(float(predicted_gain_ms) - high, 3),
            round(float(predicted_gain_ms) - low, 3),
        ],
        "prediction_within_interval": low <= predicted_gain_ms <= high,
        "measured_effect_detected": low > 0 or high < 0,
    }


def _clean(values: Sequence[float]) -> tuple[list[float], int]:
    kept = []
    dropped = 0
    for value in values:
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            kept.append(float(value))
        else:
            dropped += 1
    return kept, dropped


__all__ = ["ASSUMPTIONS", "SCHEMA", "compare_prediction", "timing_sensitivity"]
