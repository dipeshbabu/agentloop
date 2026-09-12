"""Versioned, uncalibrated savings models and self-contained evidence snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.markdown import markdown_code_span, markdown_text


@dataclass(frozen=True)
class Estimator:
    formula: str
    assumptions: tuple[str, ...]
    parameters: tuple[tuple[str, float], ...] = ()
    version: str = "1.0"

    def parameter(self, name: str) -> float:
        return dict(self.parameters)[name]


ESTIMATORS = {
    "parallelize_tools": Estimator(
        "sum_duration_ms - max_duration_ms",
        ("The formula applies assuming concurrent execution has negligible scheduling overhead.",),
    ),
    "cache_context": Estimator(
        "current_cost_usd * min(max_cost_fraction, repeated_context_ratio)",
        (
            "Repeated context can be reused without changing required outputs.",
            "The reusable share of total priced cost approximates the repeated context ratio.",
            "The provider or application supports the proposed cache.",
        ),
        (("max_cost_fraction", 0.5),),
    ),
    "add_schema_validation": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "Most observed retries are preventable with validation or repair.",
            "Added validation overhead is smaller than avoided retry time.",
        ),
        (("latency_fraction", 0.8),),
    ),
    "batch_model_calls": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "Repeated calls accept a shared batch without changing item-level outputs.",
            "Batch size, token limits, and provider throughput permit the estimated reduction.",
        ),
        (("latency_fraction", 0.35),),
    ),
    "route_to_smaller_model": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "A smaller model meets the task's quality requirements.",
            "Its serving latency is lower under comparable load.",
        ),
        (("latency_fraction", 0.25),),
    ),
    "split_large_step": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "Context can be compressed or split without losing necessary information.",
            "Extra orchestration and model calls do not consume the estimated savings.",
        ),
        (("latency_fraction", 0.20),),
    ),
    "runaway_loop": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "Some repeated steps are unnecessary for successful task completion.",
            "A guard can stop them without prematurely terminating useful work.",
        ),
        (("latency_fraction", 0.30),),
    ),
    "tool_oscillation": Estimator(
        "sum_duration_ms * latency_fraction",
        (
            "Some alternating tool calls repeat equivalent state transitions.",
            "A state-change check can remove them without losing required transitions.",
        ),
        (("latency_fraction", 0.40),),
    ),
}


def estimate_snapshot(candidate: Any, report: dict[str, Any], nodes: list[Any]) -> dict[str, Any]:
    """Copy the estimator and its inputs; never reconstruct historical records on read."""
    spec = ESTIMATORS[candidate.rule_id]
    affected = set(candidate.affected_nodes or [])
    durations = [node.duration_ms for node in nodes if node.node_id in affected]
    cost_status = report.get("cost_status", "complete")
    return {
        "estimator_id": candidate.rule_id,
        "estimator_version": spec.version,
        "method": "heuristic",
        "confidence": candidate.confidence,
        "calibrated": False,
        "formula": spec.formula,
        "parameters": dict(spec.parameters),
        "inputs": {
            "sum_duration_ms": sum(durations),
            "max_duration_ms": max(durations, default=0.0),
            "span_count": len(durations),
            "current_cost_usd": (
                report.get("estimated_cost_usd", 0.0) if is_cost_evaluable(cost_status) else None
            ),
            "repeated_context_ratio": report.get("repeated_context_ratio", 0.0),
            "cost_status": cost_status,
            "token_status": report.get("token_status", "unspecified"),
        },
        "assumptions": list(dict.fromkeys([*(candidate.assumptions or []), *spec.assumptions])),
        "unmodeled_metrics": (["latency"] if candidate.rule_id == "cache_context" else ["cost"]),
    }


def estimate_markdown(estimate: dict[str, Any] | None) -> list[str]:
    if not estimate:
        return []
    calibration = "calibrated" if estimate.get("calibrated") is True else "uncalibrated"
    return [
        f"- Estimator: {markdown_code_span(estimate['estimator_id'])} "
        f"version {markdown_text(estimate['estimator_version'])}",
        f"- Method: {markdown_text(estimate['method'])} ({calibration}; predicted savings)",
        f"- Savings formula: {markdown_code_span(estimate['formula'])}",
        "- Parameters: "
        + markdown_code_span(json.dumps(estimate.get("parameters", {}), sort_keys=True)),
        "- Assumptions: "
        + "; ".join(markdown_text(value) for value in estimate.get("assumptions", [])),
        "",
    ]
