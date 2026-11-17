"""Findings that compare repetitions of the same task.

Single-trace rules judge one run in isolation, so a detour that only some
repetitions take looks normal on its own. This module compares the repeated
attempts of each task and reports an attempt that took more model steps or
tool calls than its typical repetition and cost clearly more input tokens or
time. Each extra model step usually resends the whole context, so one extra
round can cost far more than the tool call it makes. The extra steps are the
observed difference, not a proven cause.
"""

from __future__ import annotations

import statistics
from typing import Any

from agentloop.findings import OptimizationFinding, _stable_finding_id
from agentloop.ranking import RANKING_KEY, rank_findings

FINDING_TYPE = "repetition_detour"
RULE_ID = "agentloop.repetition_detour"
RULE_VERSION = "1"
# An attempt must cost at least this much more than the median of the other
# repetitions, in input tokens or runtime, before its extra calls are reported.
HEAVIER_RATIO = 1.25
EXCERPT = 240


def _tools(trace):
    return [event for event in trace.events if event.operation_kind == "tool"]


def _models(trace):
    return sorted(
        (event for event in trace.events if event.operation_kind == "model"),
        key=lambda event: event.started_at,
    )


def _key(event):
    text = " ".join((event.input_text or "").lower().split())
    return event.name, text[:500]


def _input_tokens(trace):
    return sum(event.input_tokens for event in trace.events if event.operation_kind == "model")


def _ratio(value, typical):
    return value / typical if typical > 0 else None


def _profile(trace):
    """Scan one trace once for everything the comparison needs."""
    tools = _tools(trace)
    return {
        "trace": trace,
        "tools": tools,
        "models": _models(trace),
        "keys": {_key(event) for event in tools},
        "tokens": _input_tokens(trace),
        "runtime": float(trace.elapsed_ms or 0.0),
    }


def repetition_findings(traces: dict[tuple[str, int], Any]) -> dict[tuple[str, int], list]:
    """Return detour findings keyed by ``(task_id, repetition)``.

    Pass only completed attempts: a failed or interrupted attempt's partial
    trace would distort the typical repetition. Only tasks with at least two
    available repetitions are compared. An attempt
    is reported when it took more model steps or more tool calls than the
    median of the other repetitions, and its input tokens or runtime are at
    least 25% above the others' median.
    """
    by_task: dict[str, dict[int, Any]] = {}
    for (task_id, repetition), trace in traces.items():
        by_task.setdefault(task_id, {})[repetition] = _profile(trace)
    flagged = {}
    for task_id, attempts in by_task.items():
        if len(attempts) < 2:
            continue
        for repetition, profile in attempts.items():
            others = [attempts[other] for other in attempts if other != repetition]
            trace, tools, models = profile["trace"], profile["tools"], profile["models"]
            typical_tools = statistics.median(len(other["tools"]) for other in others)
            typical_models = statistics.median(len(other["models"]) for other in others)
            more_tools = len(tools) > typical_tools
            more_models = len(models) > typical_models
            if not (more_tools or more_models):
                continue
            other_keys = set().union(*(other["keys"] for other in others))
            # Tool calls with no counterpart elsewhere, and the model steps
            # between the first and final ones, which each resend the context.
            extra = (
                [event for event in tools if _key(event) not in other_keys] if more_tools else []
            )
            rounds = models[1:-1] if more_models else []
            if not (extra or rounds):
                continue
            tokens = profile["tokens"]
            typical_tokens = statistics.median(other["tokens"] for other in others)
            runtime = profile["runtime"]
            typical_runtime = statistics.median(other["runtime"] for other in others)
            token_ratio = _ratio(tokens, typical_tokens)
            runtime_ratio = _ratio(runtime, typical_runtime)
            # Extra model steps leave a token signature. When token counts are
            # recorded, require it; a slower run alone may be provider latency.
            # Runtime decides only for traces without token counts.
            measure = token_ratio if token_ratio is not None and tokens > 0 else runtime_ratio
            if measure is None or measure < HEAVIER_RATIO:
                continue
            flagged[(task_id, repetition)] = {
                "trace": trace,
                "extra": extra,
                "rounds": rounds,
                "tool_calls": len(tools),
                "typical_tool_calls": typical_tools,
                "model_steps": len(models),
                "typical_model_steps": typical_models,
                "repetitions_compared": len(attempts),
                "input_tokens": tokens,
                "typical_input_tokens": typical_tokens,
                "runtime_ms": runtime,
                "typical_runtime_ms": typical_runtime,
                "token_ratio": token_ratio,
                "runtime_ratio": runtime_ratio,
                "measure": measure,
            }
    return {
        key: [_finding(key, item, len(flagged))]
        for key, item in sorted(flagged.items(), key=lambda pair: pair[0])
    }


def _count(actual, typical, word):
    # Medians of an even number of repetitions can be fractional, so state the
    # observed count and the typical one instead of a fractional difference.
    plural = "" if actual == 1 else "s"
    return f"{actual} {word}{plural} (typically {typical:g})"


def _finding(key, item, flagged_attempts):
    task_id, repetition = key
    extra = [*item["rounds"], *item["extra"]]
    ratio = item["measure"]
    latency_gap = max(0.0, item["runtime_ms"] - item["typical_runtime_ms"])
    token_gap = max(0, item["input_tokens"] - round(item["typical_input_tokens"]))
    spans = [event.event_id for event in extra]
    differences = []
    if item["model_steps"] > item["typical_model_steps"]:
        differences.append(_count(item["model_steps"], item["typical_model_steps"], "model step"))
    if item["tool_calls"] > item["typical_tool_calls"]:
        differences.append(_count(item["tool_calls"], item["typical_tool_calls"], "tool call"))
    title = f"Repetition {repetition} of {task_id} took {' and '.join(differences)}"
    evidence = [
        {
            "span_id": event.event_id,
            "name": event.name,
            "event_type": event.event_type,
            "operation_kind": event.operation_kind,
            "duration_ms": event.duration_ms,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "model": event.model,
            "status": event.status,
            "input_excerpt": (event.input_text or "")[:EXCERPT],
        }
        for event in extra
    ]
    observations = {
        "task_id": task_id,
        "repetition": repetition,
        "repetitions_compared": item["repetitions_compared"],
        "input_tokens": item["input_tokens"],
        "typical_input_tokens": item["typical_input_tokens"],
        "runtime_ms": round(item["runtime_ms"], 3),
        "typical_runtime_ms": round(item["typical_runtime_ms"], 3),
        "model_steps": item["model_steps"],
        "typical_model_steps": item["typical_model_steps"],
        "tool_calls": item["tool_calls"],
        "typical_tool_calls": item["typical_tool_calls"],
        "attempts_with_a_detour": flagged_attempts,
    }
    return OptimizationFinding(
        finding_id=_stable_finding_id(FINDING_TYPE, title, spans),
        severity="high" if ratio >= 1.5 else "medium",
        type=FINDING_TYPE,
        title=title,
        confidence="medium" if flagged_attempts >= 2 else "low",
        affected_spans=spans,
        evidence=evidence,
        savings={
            "estimated_latency_savings_ms": round(latency_gap, 3),
            "estimated_cost_savings_usd": None,
            "estimated_input_tokens": token_gap,
            "formula": "attempt runtime and input tokens minus the median of the other repetitions",
        },
        rewrite={
            "kind": FINDING_TYPE,
            "hint": "Compare this attempt's steps with a typical repetition. Each extra model "
            "step resends the context, so ask the agent to gather what it needs in one step "
            "(for example, read all required files together), and remove instructions or "
            "ambiguity that trigger follow-up reads.",
            "patchable": False,
        },
        validation={
            "command": "agentloop improve finish",
            "acceptance_criteria": "repetitions take a consistent number of model steps and "
            "tool calls, and input tokens or runtime fall toward the typical repetition while "
            "quality checks pass",
        },
        metadata={
            "why": "Other repetitions of the same task finished in fewer steps.",
            "cost_status": "unknown",
            "identity": "content-v1",
        },
        evidence_level="observed",
        assumptions=[
            "Repetitions of a task receive identical inputs.",
            "The extra steps are associated with the higher cost; they are not proven to cause it.",
        ],
        observations=observations,
        rule_id=RULE_ID,
        rule_version=RULE_VERSION,
        estimate={
            "estimator_id": "repetition_median_gap",
            "estimator_version": "1",
            "formula": "attempt minus median of the other repetitions",
            "unmodeled_metrics": ["cost"],
            "assumptions": ["Savings assume the attempt can match its typical repetition."],
        },
    ).to_dict()


def add_repetition_findings(diagnoses, traces):
    """Merge detour findings into each attempt's diagnosis and re-rank it."""
    for key, extra in repetition_findings(traces).items():
        diagnosis = diagnoses.get(key)
        if diagnosis is None:
            continue
        trace = traces[key]
        combined = [*diagnosis["findings"], *extra]
        diagnosis["findings"] = rank_findings(
            combined, inputs=trace.metadata.get(RANKING_KEY, {}), sort_by="priority"
        )
        diagnosis["summary"]["finding_count"] = len(combined)
        diagnosis["summary"]["high_severity_count"] = sum(
            finding["severity"] == "high" for finding in combined
        )
        diagnosis["ranking"]["ready_to_test_count"] = sum(
            finding["ranking"]["status"] == "ready_to_test" for finding in diagnosis["findings"]
        )
    return diagnoses
