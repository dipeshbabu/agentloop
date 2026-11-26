"""Bounded retry and loop optimization passes (issue #401).

Not every retry is waste. A timeout that succeeds on the second attempt is the
retry doing its job; re-sending a request the tool rejected for bad input is
not. This module separates those cases, proposes bounded guard limits for the
wasteful ones, and checks each proposal against recorded runs before anyone
enables it.

``classify_retries(trace)``
    Groups consecutive attempts of the same step and labels each group's
    cause: ``transport``, ``schema``, ``semantic``, ``tool`` or ``unknown``.
    Transport and semantic retries are ``legitimate``; repeated schema repair
    and repeated identical tool failures are ``candidate``; retries of a tool
    that isn't declared idempotent are flagged ``unsafe_retry``.

``find_loops(trace)``
    Identical calls repeated without declared progress, and A/B oscillation.

``propose_limits(traces)``
    A ``LoopLimits`` candidate built from the evidence, or an abstention. Each
    limit is the smallest that still admits every recorded run that completed.

``replay_limits(limits, trace)``
    Where the guard would have stopped a recorded run, if at all.

``compare_runs(baseline, candidate)``
    Baseline versus candidate runs with completion contracts. A faster
    candidate that completes less often is rejected.

Candidates are always returned as a shadow-mode policy. Enforcing one is a
separate, explicit host decision.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from agentloop.execution_ir import build_execution_ir
from agentloop.harness import HarnessConfig
from agentloop.loop_guards import LoopLimits, loop_guard_policy

SCHEMA = "agentloop.retry_pass/v1"

TRANSPORT = {
    "timeout",
    "timed_out",
    "connection_error",
    "connection_reset",
    "rate_limited",
    "rate_limit",
    "service_unavailable",
    "network",
    "transport",
}
SCHEMA_CAUSES = {"invalid_output_schema", "schema_validation_failed", "json_decode_error", "schema"}
SEMANTIC = {"insufficient_answer", "quality_check_failed", "semantic", "verification_failed"}
TOOL = {"tool_error", "invalid_arguments", "bad_request", "not_found", "permission_denied"}
_TRANSPORT_ERRORS = ("TimeoutError", "ConnectionError", "ConnectionResetError", "429", "503")
_SCHEMA_ERRORS = ("JSONDecodeError", "ValidationError", "SchemaError")


def _cause(event: Any) -> str:
    metadata = event.metadata if isinstance(getattr(event, "metadata", None), Mapping) else {}
    declared = metadata.get("retry_reason", metadata.get("reason", metadata.get("error_type")))
    if isinstance(declared, str):
        value = declared.strip().lower()
        for label, names in (
            ("transport", TRANSPORT),
            ("schema", SCHEMA_CAUSES),
            ("semantic", SEMANTIC),
            ("tool", TOOL),
        ):
            if value in names:
                return label
    error = getattr(event, "error", None) or ""
    if any(marker in error for marker in _TRANSPORT_ERRORS):
        return "transport"
    if any(marker in error for marker in _SCHEMA_ERRORS):
        return "schema"
    return "unknown"


def _step_key(event: Any) -> tuple[str, Any, Any]:
    metadata = event.metadata if isinstance(getattr(event, "metadata", None), Mapping) else {}
    return (event.name, event.parent_id, metadata.get("tool_reuse_key", metadata.get("input_hash")))


def classify_retries(trace: Any) -> dict[str, Any]:
    """Group consecutive attempts of each step and label why they were retried."""
    ir = build_execution_ir(trace)
    idempotent = {op["id"]: op["idempotent"] for op in ir["operations"]}
    events = sorted(
        (e for e in trace.events if e.event_type in {"tool_call", "model_call", "retry"}),
        key=lambda e: (e.started_at or "", e.event_id),
    )
    groups: list[dict[str, Any]] = []
    open_groups: dict[tuple[str, Any, Any], dict[str, Any]] = {}
    for event in events:
        key = _step_key(event)
        if event.event_type == "retry":
            groups.append(_retry_span_group(event))
            continue
        group = open_groups.get(key)
        if group is not None and group["_last_status"] == "error":
            group["attempts"].append(event.event_id)
            group["causes"].append(group["_pending_cause"])
            group["retry_ms"] += event.duration_ms or 0.0
        else:
            group = {
                "step": event.name,
                "parent": event.parent_id,
                "attempts": [event.event_id],
                "causes": [],
                "retry_ms": 0.0,
                "idempotent": idempotent.get(event.event_id, {"value": None, "basis": "unknown"}),
            }
            open_groups[key] = group
            groups.append(group)
        group["_last_status"] = event.status
        group["_pending_cause"] = _cause(event) if event.status == "error" else None
        group["outcome"] = "succeeded" if event.status == "ok" else "failed"
    results = []
    for group in groups:
        group.pop("_last_status", None)
        group.pop("_pending_cause", None)
        if len(group["attempts"]) < 2 and group.get("kind") != "retry_span":
            continue
        results.append(_verdict(group))
    return {"schema": SCHEMA, "groups": results, "summary": _summary(results)}


def _retry_span_group(event: Any) -> dict[str, Any]:
    return {
        "step": event.name,
        "parent": event.parent_id,
        "attempts": [event.event_id],
        "causes": [_cause(event)],
        "retry_ms": event.duration_ms or 0.0,
        "idempotent": {"value": None, "basis": "unknown"},
        "outcome": "succeeded" if event.status == "ok" else "failed",
        "kind": "retry_span",
    }


def _verdict(group: dict[str, Any]) -> dict[str, Any]:
    causes = set(group["causes"])
    retries = len(group["causes"])
    cause = causes.pop() if len(causes) == 1 else ("mixed" if causes else "unknown")
    reasons = []
    if cause in {"transport", "semantic"}:
        verdict = "legitimate"
        reasons.append(f"{cause} failures are what retries are for")
    elif cause == "schema" and retries >= 2:
        verdict = "candidate"
        reasons.append("repeated full retries for schema failures; a cheaper repair may do")
    elif cause == "tool" and group.get("kind") != "retry_span":
        verdict = "candidate"
        reasons.append("the same call failed again with a non-transient tool error")
    else:
        verdict = "unknown"
        reasons.append("the cause of these retries is not declared or not consistent")
    if group["idempotent"].get("value") is not True and group.get("kind") != "retry_span":
        # Even a timeout retry can repeat a side effect, such as a second charge.
        reasons.append(
            "the tool is not declared idempotent, so retrying it may repeat a side effect"
        )
        verdict = "unsafe_retry"
        group["unsafe"] = True
    return {
        **group,
        "cause": cause,
        "retries": retries,
        "retry_ms": round(group["retry_ms"], 3),
        "verdict": verdict,
        "reasons": reasons,
    }


def _summary(groups: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for group in groups:
        counts[group["verdict"]] = counts.get(group["verdict"], 0) + 1
    return {
        "groups": len(groups),
        "by_verdict": dict(sorted(counts.items())),
        "candidate_retry_ms": round(
            sum(g["retry_ms"] for g in groups if g["verdict"] == "candidate"), 3
        ),
    }


# Loops ----------------------------------------------------------------------------


def _calls(trace: Any) -> list[tuple[str, str]]:
    """Each tool call as (step, input key) in start order."""
    events = sorted(
        (e for e in trace.events if e.event_type == "tool_call"),
        key=lambda e: (e.started_at or "", e.event_id),
    )
    calls = []
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
        key = metadata.get("tool_reuse_key", metadata.get("input_hash"))
        calls.append((event.name, key if isinstance(key, str) else event.event_id))
    return calls


def find_loops(trace: Any) -> dict[str, Any]:
    """Identical repeated calls and A/B oscillation in one recorded run."""
    calls = _calls(trace)
    identical: dict[tuple[str, str], int] = {}
    for call in calls:
        identical[call] = identical.get(call, 0) + 1
    repeated = [
        {"step": step, "identical_calls": count}
        for (step, key), count in sorted(identical.items())
        if count >= 3 and not key.startswith("evt_")
    ]
    names = [step for step, _ in calls]
    oscillation = _longest_oscillation(names)
    return {"repeated_identical_calls": repeated, "oscillation": oscillation}


def _longest_oscillation(names: list[str]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for period in range(2, 5):
        for start in range(len(names)):
            pattern = names[start : start + period]
            if len(pattern) < period or len(set(pattern)) < 2:
                continue
            repeats = 0
            while names[start + repeats * period : start + (repeats + 1) * period] == pattern:
                repeats += 1
            if repeats >= 3 and (best is None or repeats > best["repeats"]):
                best = {"pattern": pattern, "period": period, "repeats": repeats}
    return best


# Replaying a limit on a recorded run ----------------------------------------------------


def replay_limits(limits: LoopLimits, trace: Any) -> dict[str, Any]:
    """Where a guard with ``limits`` would have stopped this run, if at all.

    Mirrors the guard: retries are counted per step, identical calls per
    (step, input key), and oscillation over the step sequence.
    """
    if type(limits) is not LoopLimits:
        raise ValueError("limits must be LoopLimits")
    calls = _calls(trace)
    retries = classify_retries(trace)["groups"]
    for group in retries:
        if (
            limits.max_retries_per_step is not None
            and group["retries"] > limits.max_retries_per_step
        ):
            return {"stopped": True, "reason": "retry_limit", "step": group["step"]}
    seen: dict[tuple[str, str], int] = {}
    history: list[str] = []
    for index, call in enumerate(calls):
        seen[call] = seen.get(call, 0) + 1
        if (
            limits.max_identical_calls is not None
            and not call[1].startswith("evt_")
            and seen[call] > limits.max_identical_calls
        ):
            return {"stopped": True, "reason": "identical_calls", "step": call[0], "at": index}
        history.append(call[0])
        if limits.oscillation_period is not None:
            for period in range(2, limits.oscillation_period + 1):
                size = period * limits.oscillation_repeats
                tail = history[-size:]
                if (
                    len(tail) == size
                    and len(set(tail[:period])) > 1
                    and all(tail[i] == tail[i % period] for i in range(size))
                ):
                    return {"stopped": True, "reason": "oscillation", "step": call[0], "at": index}
    return {"stopped": False}


# Proposals ---------------------------------------------------------------------------


def propose_limits(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Propose bounded ``LoopLimits`` from recorded runs, or abstain.

    Each run is ``{"trace": AgentTrace, "completed": bool}``, where
    ``completed`` is the run's own completion contract. A limit is proposed
    only for evidence of waste, and is never tighter than what any completed
    run needed. Without a completed run, the pass abstains.
    """
    if not runs:
        return {"schema": SCHEMA, "status": "abstained", "reasons": ["no runs were supplied"]}
    for run in runs:
        if not isinstance(run, Mapping) or not isinstance(run.get("completed"), bool):
            raise ValueError("each run needs a trace and a boolean 'completed'")
    completed = [run["trace"] for run in runs if run["completed"]]
    if not completed:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "reasons": ["no run completed, so there is no safe lower bound for any limit"],
        }
    evidence = [classify_retries(run["trace"]) for run in runs]
    loops = [find_loops(run["trace"]) for run in runs]
    reasons: list[str] = []
    changes: dict[str, Any] = {}

    wasted = [g for result in evidence for g in result["groups"] if g["verdict"] == "candidate"]
    unsafe = [g for result in evidence for g in result["groups"] if g.get("unsafe")]
    if wasted:
        needed = max(
            (g["retries"] for t in completed for g in classify_retries(t)["groups"]), default=0
        )
        changes["max_retries_per_step"] = max(needed, 1)
        reasons.append(
            f"{len(wasted)} retry group(s) repeated schema or tool failures; completed runs "
            f"needed at most {needed} retr{'y' if needed == 1 else 'ies'} per step"
        )
    repeated = [item for result in loops for item in result["repeated_identical_calls"]]
    if repeated:
        needed = max(
            (
                item["identical_calls"]
                for t in completed
                for item in find_loops(t)["repeated_identical_calls"]
            ),
            default=2,
        )
        changes["max_identical_calls"] = max(needed, 2)
        reasons.append("identical calls repeated without new input")
    oscillations = [result["oscillation"] for result in loops if result["oscillation"]]
    if oscillations:
        period = max(item["period"] for item in oscillations)
        needed = max(
            ((find_loops(t)["oscillation"] or {}).get("repeats", 0) for t in completed), default=0
        )
        changes["oscillation_period"] = period
        changes["oscillation_repeats"] = min(8, max(3, needed + 1))
        reasons.append(f"calls alternated with period {period}")
    if not changes:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "reasons": ["no wasteful retries or loops were found; legitimate retries are kept"],
            "unsafe_retries": [g["step"] for g in unsafe],
        }
    limits = LoopLimits(**{"max_retries_per_step": None, **changes})
    blocked = [
        index for index, trace in enumerate(completed) if replay_limits(limits, trace)["stopped"]
    ]
    if blocked:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "reasons": [f"the limits would stop completed run(s) {blocked}"],
        }
    return {
        "schema": SCHEMA,
        "status": "candidate",
        "limits": {
            "max_retries_per_step": limits.max_retries_per_step,
            "max_identical_calls": limits.max_identical_calls,
            "oscillation_period": limits.oscillation_period,
            "oscillation_repeats": limits.oscillation_repeats,
        },
        "reasons": reasons,
        "unsafe_retries": sorted({g["step"] for g in unsafe}),
        "would_stop": [
            index
            for index, run in enumerate(runs)
            if not run["completed"] and replay_limits(limits, run["trace"])["stopped"]
        ],
        "mode": "shadow",
    }


def shadow_policy(candidate: Mapping[str, Any]) -> HarnessConfig:
    """A shadow-mode harness config for a candidate. Enforcing it is the host's decision."""
    if candidate.get("status") != "candidate":
        raise ValueError("only a candidate can become a policy")
    limits = LoopLimits(**candidate["limits"])
    return HarnessConfig(mode="shadow", policies=(loop_guard_policy(limits),))


def compare_runs(
    baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Compare completion and runtime. ``{"runtime_ms": float, "completed": bool}`` per run.

    A candidate that completes less often than the baseline is rejected however
    much faster it is; that is the negative control for stopping too early.
    """

    def summary(runs):
        valid = [r for r in runs if isinstance(r.get("completed"), bool)]
        times = [
            float(r["runtime_ms"])
            for r in valid
            if r["completed"] and isinstance(r.get("runtime_ms"), int | float)
        ]
        return {
            "runs": len(valid),
            "completed": sum(1 for r in valid if r["completed"]),
            "completion_rate": (sum(1 for r in valid if r["completed"]) / len(valid))
            if valid
            else None,
            "median_completed_runtime_ms": statistics.median(times) if times else None,
        }

    base, cand = summary(baseline), summary(candidate)
    if not base["runs"] or not cand["runs"]:
        verdict = "insufficient_evidence"
    elif cand["completion_rate"] < base["completion_rate"]:
        verdict = "rejected_incomplete"
    elif (
        base["median_completed_runtime_ms"] is not None
        and cand["median_completed_runtime_ms"] is not None
        and cand["median_completed_runtime_ms"] < base["median_completed_runtime_ms"]
    ):
        verdict = "improved"
    else:
        verdict = "no_gain"
    return {"baseline": base, "candidate": cand, "verdict": verdict, "evidence": "measured"}


__all__ = [
    "SCHEMA",
    "classify_retries",
    "compare_runs",
    "find_loops",
    "propose_limits",
    "replay_limits",
    "shadow_policy",
]
