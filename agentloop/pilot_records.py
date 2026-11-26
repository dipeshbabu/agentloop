"""Developer pilot records and optimization-method comparisons (part of issue #406).

Two small, validated formats for evidence that only people can produce:

``pilot record``
    One consenting developer's first use of AgentLoop on their own agent, as
    described in docs/ADOPTION_GAPS.md: when they started, when they got a
    valid trace and a useful finding, what failed, what help they needed, and
    whether the change they tried was accepted or rejected. ``summarize_pilot``
    reports medians and rates with their denominators. Records carry an opaque
    participant id and a consent reference, never names or contact details.

``method result``
    One optimization method on one workload of a frozen benchmark: the
    method (no optimization, the current heuristic ranking, a manual change, or
    an AgentLoop pass), the split, whether the workload is a negative control,
    and independently scored quality and measured cost. ``compare_methods``
    compares every method with the ``no_optimization`` baseline on the same
    held-out workloads.

Neither module ships any data. No participant observations exist until a
maintainer records them with consent.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, overload

PILOT_SCHEMA = "agentloop.pilot_record/v1"
METHOD_SCHEMA = "agentloop.method_result/v1"
OUTCOMES = ("accepted", "rejected", "abandoned", "in_progress")
METHOD_KINDS = ("no_optimization", "heuristic_ranking", "manual", "agentloop_pass")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_FORBIDDEN = {"name", "email", "phone", "address", "company", "github", "username"}


class RecordError(ValueError):
    """A record is malformed or contains fields it must not."""


@overload
def _time(value: Any, label: str, *, optional: Literal[False] = False) -> datetime: ...
@overload
def _time(value: Any, label: str, *, optional: bool) -> datetime | None: ...
def _time(value: Any, label: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise RecordError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError(f"{label} must be an ISO timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise RecordError(f"{label} must be a short identifier (letters, digits, _.:-)")
    return value


def validate_pilot_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Check one pilot record and return it with derived minutes."""
    if not isinstance(record, Mapping):
        raise RecordError("a pilot record must be a mapping")
    personal = sorted(_FORBIDDEN & {str(key).lower() for key in record})
    if personal:
        raise RecordError(f"pilot records must not contain personal fields: {personal}")
    if record.get("schema") != PILOT_SCHEMA:
        raise RecordError(f"schema must be {PILOT_SCHEMA}")
    _identifier(record.get("participant"), "participant")
    consent = record.get("consent_ref")
    if not isinstance(consent, str) or not consent.strip():
        raise RecordError("consent_ref is required: record where consent is documented")
    _identifier(record.get("application_category"), "application_category")
    if not isinstance(record.get("agentloop_version"), str):
        raise RecordError("agentloop_version is required")
    started = _time(record.get("started_at"), "started_at")
    milestones: dict[str, datetime | None] = {}
    for key in ("first_valid_trace_at", "first_useful_finding_at", "outcome_at"):
        moment = _time(record.get(key), key, optional=True)
        if moment is not None and moment < started:
            raise RecordError(f"{key} is before started_at")
        milestones[key] = moment
    first_trace = milestones["first_valid_trace_at"]
    first_finding = milestones["first_useful_finding_at"]
    if first_trace is not None and first_finding is not None and first_finding < first_trace:
        raise RecordError("first_useful_finding_at is before first_valid_trace_at")
    outcome = record.get("outcome")
    if outcome not in OUTCOMES:
        raise RecordError(f"outcome must be one of {', '.join(OUTCOMES)}")
    if outcome in {"accepted", "rejected"} and milestones["outcome_at"] is None:
        raise RecordError("an accepted or rejected outcome needs outcome_at")
    for key in ("failed_steps", "assistance"):
        value = record.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RecordError(f"{key} must be a list of short descriptions")
    effort = record.get("effort_minutes")
    if effort is not None and (
        isinstance(effort, bool) or not isinstance(effort, int | float) or effort < 0
    ):
        raise RecordError("effort_minutes must be a non-negative number")

    def minutes(key: str) -> float | None:
        moment = milestones[key]
        return None if moment is None else round((moment - started).total_seconds() / 60, 2)

    return {
        **dict(record),
        "minutes_to_first_valid_trace": minutes("first_valid_trace_at"),
        "minutes_to_first_useful_finding": minutes("first_useful_finding_at"),
        "minutes_to_outcome": minutes("outcome_at"),
    }


def summarize_pilot(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Medians and rates over validated records, each with its denominator."""
    checked = [validate_pilot_record(record) for record in records]
    participants = [r["participant"] for r in checked]
    if len(participants) != len(set(participants)):
        raise RecordError("each participant may appear once per summary")

    def median_of(key):
        values = [r[key] for r in checked if r[key] is not None]
        return {
            "median": round(statistics.median(values), 2) if values else None,
            "reached": len(values),
            "of": len(checked),
        }

    outcomes = {outcome: sum(1 for r in checked if r["outcome"] == outcome) for outcome in OUTCOMES}
    decided = outcomes["accepted"] + outcomes["rejected"]
    return {
        "schema": PILOT_SCHEMA,
        "participants": len(checked),
        "first_valid_trace": median_of("minutes_to_first_valid_trace"),
        "first_useful_finding": median_of("minutes_to_first_useful_finding"),
        "outcome": median_of("minutes_to_outcome"),
        "outcomes": outcomes,
        "acceptance_rate": {
            "value": round(outcomes["accepted"] / decided, 4) if decided else None,
            "accepted": outcomes["accepted"],
            "of_decided": decided,
        },
        "needed_assistance": sum(1 for r in checked if r.get("assistance")),
        "with_failed_steps": sum(1 for r in checked if r.get("failed_steps")),
        "limits": "Small consenting pilot; not a population estimate.",
    }


def validate_method_result(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping) or row.get("schema") != METHOD_SCHEMA:
        raise RecordError(f"each method result needs schema {METHOD_SCHEMA}")
    _identifier(row.get("workload"), "workload")
    _identifier(row.get("method"), "method")
    if row.get("method_kind") not in METHOD_KINDS:
        raise RecordError(f"method_kind must be one of {', '.join(METHOD_KINDS)}")
    if row.get("split") not in {"development", "held_out"}:
        raise RecordError("split must be 'development' or 'held_out'")
    if not isinstance(row.get("negative_control"), bool):
        raise RecordError("negative_control must be true or false")
    if row.get("quality_source") not in {"independent", "self_reported"}:
        raise RecordError("quality_source must be 'independent' or 'self_reported'")
    for key in ("quality", "runtime_ms", "cost_usd"):
        value = row.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float) or value < 0
        ):
            raise RecordError(f"{key} must be a non-negative number or null")
    if row.get("quality") is None:
        raise RecordError("quality is required")
    return dict(row)


def compare_methods(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare each method with ``no_optimization`` on held-out workloads.

    Only held-out rows count. A method's gain on a workload is the baseline's
    runtime minus its own; a quality drop below the baseline is a regression.
    Negative-control workloads, where nothing should improve, are reported
    separately: a method that "improves" them points to measurement noise or
    leakage. Rows whose quality was self-reported are counted but not scored.
    """
    checked = [validate_method_result(row) for row in rows]
    keys = [(r["workload"], r["method"]) for r in checked if r["split"] == "held_out"]
    if len(keys) != len(set(keys)):
        raise RecordError("each method may have one held-out result per workload")
    held = [r for r in checked if r["split"] == "held_out" and r["quality_source"] == "independent"]
    baseline = {r["workload"]: r for r in held if r["method_kind"] == "no_optimization"}
    methods = sorted({r["method"] for r in held if r["method_kind"] != "no_optimization"})
    report = []
    for method in methods:
        mine = [r for r in held if r["method"] == method]
        compared = [(r, baseline[r["workload"]]) for r in mine if r["workload"] in baseline]
        regular = [(r, b) for r, b in compared if not r["negative_control"]]
        controls = [(r, b) for r, b in compared if r["negative_control"]]
        gains = [
            b["runtime_ms"] - r["runtime_ms"]
            for r, b in regular
            if r["runtime_ms"] is not None and b["runtime_ms"] is not None
        ]
        report.append(
            {
                "method": method,
                "method_kind": mine[0]["method_kind"],
                "workloads": len(regular),
                "without_baseline": len(mine) - len(compared),
                "quality_regressions": sum(1 for r, b in regular if r["quality"] < b["quality"]),
                "median_runtime_gain_ms": round(statistics.median(gains), 3) if gains else None,
                "runtime_known": len(gains),
                "cost_known": sum(
                    1 for r, b in regular if r["cost_usd"] is not None and b["cost_usd"] is not None
                ),
                "negative_controls": len(controls),
                "negative_controls_improved": sum(
                    1
                    for r, b in controls
                    if r["runtime_ms"] is not None
                    and b["runtime_ms"] is not None
                    and r["runtime_ms"] < b["runtime_ms"]
                ),
            }
        )
    return {
        "schema": METHOD_SCHEMA,
        "held_out_workloads": len(baseline),
        "self_reported_rows_excluded": sum(
            1 for r in checked if r["split"] == "held_out" and r["quality_source"] != "independent"
        ),
        "development_rows_excluded": sum(1 for r in checked if r["split"] == "development"),
        "methods": report,
    }


__all__ = [
    "METHOD_KINDS",
    "METHOD_SCHEMA",
    "OUTCOMES",
    "PILOT_SCHEMA",
    "RecordError",
    "compare_methods",
    "summarize_pilot",
    "validate_method_result",
    "validate_pilot_record",
]
