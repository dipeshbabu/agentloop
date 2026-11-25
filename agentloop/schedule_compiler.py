"""Compile recorded tool calls into an explicit, bounded concurrent schedule (issue #400).

``compile_schedule`` looks at each group of sibling tool calls in a recorded run
(calls under the same parent) and asks whether the existing ``ToolScheduler``
could run them concurrently. It only uses *declared* evidence from the
execution IR:

* ``depends_on`` (plain metadata or ``agentloop.stage``),
* ``agentloop.effects``: ``effect`` and the ``reads`` / ``writes`` state keys,
* ``parallel_safe``.

A call with an unknown dependency list, unknown reads or writes, an
``external`` or unknown effect, or ``parallel_safe: false`` makes its group
abstain. The observed timing is never used to infer independence.

For a group that qualifies, the result carries the scheduler's own plan
(effective dependencies, resource conflicts, plan hash) and a simulated
makespan for that plan under ``max_concurrency``, from the observed
durations. The simulated gain is a hypothesis; measure it with
``timing_sensitivity.compare_prediction``.

``tool_calls(group, tools)`` turns a qualifying group into ``ToolCall``
declarations bound to your callables, ready for ``ToolScheduler.execute``.
Result order, failure propagation, cancellation and deadlines are the
scheduler's existing behaviour.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Callable, Mapping
from typing import Any

from agentloop.execution_ir import build_execution_ir
from agentloop.scheduling_plan import plan_tools
from agentloop.scheduling_types import ScheduleConfig, SchedulingError, ToolCall

SCHEMA = "agentloop.schedule_candidate/v1"
_EFFECT = {"none": "read_only", "read": "read_only", "write": "mutating"}
_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _noop(context: Any) -> None:  # placeholder callback for planning only
    return None


def compile_schedule(source: Any, *, max_concurrency: int = 4) -> dict[str, Any]:
    """Find sibling tool groups the scheduler could run concurrently. Never executes."""
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
        raise ValueError("max_concurrency must be an integer from 1 to 32")
    ir = source if isinstance(source, Mapping) else build_execution_ir(source)
    operations = ir.get("operations", [])
    flagged: dict[str, list[str]] = {}
    for item in ir.get("unsupported", []):
        flagged.setdefault(item["operation"], []).append(item["reason"])

    groups: dict[Any, list[dict[str, Any]]] = {}
    for operation in operations:
        if operation["kind"] == "tool":
            groups.setdefault(operation["parent"], []).append(operation)

    results = []
    for parent, members in groups.items():
        if len(members) < 2:
            continue
        results.append(_group(parent, members, flagged, max_concurrency))
    return {
        "schema": SCHEMA,
        "evidence": "simulated",
        "max_concurrency": max_concurrency,
        "groups": results,
        "candidates": sum(1 for group in results if group["status"] == "candidate"),
    }


def _group(parent, members, flagged, max_concurrency) -> dict[str, Any]:
    ids = [member["id"] for member in members]
    member_set = set(ids)
    reasons: list[str] = []
    calls: list[ToolCall] = []
    durations: dict[str, float] = {}
    declarations: dict[str, dict[str, Any]] = {}
    for member in members:
        op_id = member["id"]
        for reason in flagged.get(op_id, []):
            reasons.append(f"{op_id}: {reason}")
        if not _CALL_ID.match(op_id):
            reasons.append(f"{op_id}: span id cannot be used as a scheduler call id")
            continue
        declared = member.get("depends_on", {})
        depends = declared.get("value") if declared.get("basis") == "declared" else None
        if depends is None:
            reasons.append(f"{op_id}: dependencies are not declared")
        else:
            # Dependencies outside this group finished before the group started.
            depends = [source for source in depends if source in member_set]
        effect = member["effect"]
        if effect["basis"] != "declared":
            reasons.append(f"{op_id}: effect is not declared")
        elif effect["value"] not in _EFFECT:
            reasons.append(f"{op_id}: effect {effect['value']!r} cannot be scheduled concurrently")
        reads, writes = member["reads"], member["writes"]
        read_keys = reads["value"] if reads["basis"] == "declared" else None
        write_keys = writes["value"] if writes["basis"] == "declared" else None
        if effect.get("value") in {"none", "read"} and write_keys is None:
            write_keys = []  # a declared read-only call writes nothing
        if read_keys is None and effect.get("value") == "none":
            read_keys = []
        if read_keys is None:
            reasons.append(f"{op_id}: state reads are not declared")
        if write_keys is None:
            reasons.append(f"{op_id}: state writes are not declared")
        if effect.get("value") == "write" and not write_keys:
            reasons.append(f"{op_id}: a write effect must declare the state keys it writes")
        concurrent = member["parallel_safe"]
        if concurrent["basis"] == "declared" and concurrent["value"] is False:
            reasons.append(f"{op_id}: declared parallel_safe: false")
        timing = member["timing"]["value"]
        if timing is None:
            reasons.append(f"{op_id}: timing is missing")
        else:
            durations[op_id] = timing["end_ms"] - timing["start_ms"]
        declarations[op_id] = {
            "depends_on": depends,
            "reads": read_keys,
            "writes": write_keys,
            "effect": _EFFECT.get(effect.get("value"), "unknown"),
        }
        if not reasons:
            calls.append(
                ToolCall(
                    op_id,
                    _noop,
                    depends_on=tuple(depends or ()),
                    reads=tuple(read_keys or ()),
                    writes=tuple(write_keys or ()),
                    concurrent=True,
                    effect=_EFFECT[effect["value"]],
                )
            )
    base = {"parent": parent, "operations": ids, "declarations": declarations}
    if reasons:
        return {**base, "status": "abstained", "reasons": reasons}
    config = ScheduleConfig("compiled", "1", "tools", max_concurrency=max_concurrency)
    try:
        plan = plan_tools(calls, config, enforce=False)
    except SchedulingError as error:
        return {
            **base,
            "status": "abstained",
            "reasons": [f"scheduler refused: {error.reason_code}"],
        }
    effective = plan["effective_dependencies"]
    observed = _observed_span(members)
    predicted = _list_schedule(plan["serial_order"], effective, durations, max_concurrency)
    lower_bound = _critical_path(plan["serial_order"], effective, durations)
    gain = max(0.0, observed - predicted)
    status = "candidate" if gain > 0 else "no_gain"
    return {
        **base,
        "status": status,
        "reasons": []
        if status == "candidate"
        else ["the declared dependencies and conflicts leave nothing to overlap"],
        "plan_hash": plan["plan_hash"],
        "serial_order": plan["serial_order"],
        "effective_dependencies": effective,
        "resource_conflicts": plan["resource_conflicts"],
        "observed_ms": round(observed, 3),
        "predicted_ms": round(predicted, 3),
        "critical_path_ms": round(lower_bound, 3),
        "predicted_gain_ms": round(gain, 3),
        "evidence": "simulated",
    }


def _observed_span(members) -> float:
    timings = [m["timing"]["value"] for m in members]
    return max(t["end_ms"] for t in timings) - min(t["start_ms"] for t in timings)


def _critical_path(order, dependencies, durations) -> float:
    finish: dict[str, float] = {}
    for op_id in order:
        start = max((finish[source] for source in dependencies[op_id]), default=0.0)
        finish[op_id] = start + durations[op_id]
    return max(finish.values(), default=0.0)


def _list_schedule(order, dependencies, durations, slots) -> float:
    """Greedy list scheduling in serial order with at most ``slots`` concurrent calls."""
    rank = {op_id: index for index, op_id in enumerate(order)}
    remaining = {op_id: set(dependencies[op_id]) for op_id in order}
    successors: dict[str, list[str]] = {op_id: [] for op_id in order}
    for op_id in order:
        for source in dependencies[op_id]:
            successors[source].append(op_id)
    ready = [rank[op_id] for op_id in order if not remaining[op_id]]
    heapq.heapify(ready)
    running: list[tuple[float, str]] = []
    finish: dict[str, float] = {}
    clock = 0.0
    while ready or running:
        while ready and len(running) < slots:
            op_id = order[heapq.heappop(ready)]
            heapq.heappush(running, (clock + durations[op_id], op_id))
        clock, done = heapq.heappop(running)
        finish[done] = clock
        for op_id in successors[done]:
            remaining[op_id].discard(done)
            if not remaining[op_id]:
                heapq.heappush(ready, rank[op_id])
    return max(finish.values(), default=0.0)


def tool_calls(group: Mapping[str, Any], tools: Mapping[str, Callable[..., Any]]) -> list[ToolCall]:
    """``ToolCall`` declarations for a candidate group, bound to ``tools[span_id]``.

    Every span in the group must have a callable; declarations come from the
    compiled group unchanged, so the scheduler re-derives the same plan.
    """
    if group.get("status") not in {"candidate", "no_gain"}:
        raise ValueError("only a compiled candidate group can be turned into tool calls")
    missing = [op_id for op_id in group["serial_order"] if op_id not in tools]
    if missing:
        raise ValueError(f"no callable for {', '.join(missing)}")
    calls = []
    for op_id in group["operations"]:
        declared = group["declarations"][op_id]
        calls.append(
            ToolCall(
                op_id,
                tools[op_id],
                depends_on=tuple(declared["depends_on"] or ()),
                reads=tuple(declared["reads"]),
                writes=tuple(declared["writes"]),
                concurrent=True,
                effect=declared["effect"],
            )
        )
    return calls


__all__ = ["SCHEMA", "compile_schedule", "tool_calls"]
