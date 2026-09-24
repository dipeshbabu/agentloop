"""Versioned task scorers; labels are never given to the agent implementation."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

SCORER_VERSION = "real-agent-task-1.0"


def _numeric_equal(left, right):
    if type(left) not in {int, float} or type(right) not in {int, float}:
        return False
    return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= 1e-6


def grade(output, task, *, sources, tool_history):
    """Success means the frozen task criterion, including the required tool use."""
    if not isinstance(output, dict):
        return {"score": 0.0, "passed": False, "detail": "missing or malformed final object"}
    workload = task["workload"]
    successful = [item for item in tool_history if item["status"] == "ok"]
    passed = False
    if workload == "repository":
        expected = task["expected"]
        path, symbol = output.get("path"), output.get("symbol")
        line, quote = output.get("evidence_line"), output.get("evidence")
        lines = sources.get(path, "").splitlines() if isinstance(path, str) else []
        passed = (
            path == expected["path"]
            and symbol == expected["symbol"]
            and type(line) is int
            and expected["start"] <= line <= expected["end"]
            and 1 <= line <= len(lines)
            and isinstance(quote, str)
            and len(quote.strip()) >= 8
            and quote in lines[line - 1]
            and any(item["tool"] in {"search", "read"} for item in successful)
        )
    elif workload == "sql":
        actual, expected = output.get("rows"), task["expected"]
        passed = (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                isinstance(row, list) and len(row) == len(reference)
                for row, reference in zip(actual, expected)
            )
        )
        if passed:
            passed = all(
                _numeric_equal(value, reference)
                if type(reference) in {int, float}
                else type(value) is type(reference) and value == reference
                for row, expected_row in zip(actual, expected)
                for value, reference in zip(row, expected_row)
            ) and any(item["tool"] == "sql" for item in successful)
    elif workload == "math":
        try:
            answer = output.get("answer")
            valid_type = type(answer) in {int, float, str}
            value = Decimal(str(answer).replace(",", "")) if valid_type else Decimal("NaN")
            expected = Decimal(task["expected"])
            passed = (
                value.is_finite()
                and value == expected
                and any(
                    item["tool"] == "calculate" and Decimal(str(item["result"])) == expected
                    for item in successful
                )
            )
        except (InvalidOperation, ValueError):
            passed = False
    else:
        raise ValueError("unsupported workload")
    return {
        "score": float(bool(passed)),
        "passed": bool(passed),
        "detail": f"{SCORER_VERSION}: independent {workload} criterion {'passed' if passed else 'failed'}",
    }


def replay_scorer(output, fixture, scorer):
    """Recompute replay quality from retained answers/tools and frozen labels."""
    if not isinstance(output, dict) or not isinstance(output.get("tool_history"), list):
        return {"score": 0.0, "passed": False, "detail": "missing original task output"}
    if scorer.get("version") != SCORER_VERSION:
        raise ValueError("task scorer version mismatch")
    if output.get("status") != "completed":
        return {"score": 0.0, "passed": False, "detail": "task did not finish within its bounds"}
    return grade(
        output.get("answer"),
        fixture["task"],
        sources=fixture.get("sources", {}),
        tool_history=output["tool_history"],
    )
