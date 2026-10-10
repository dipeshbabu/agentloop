"""Measurement qualifications for native projections of external evidence.

Native event schema requires numeric durations and two statuses. Missing source
measurements use compatibility placeholders, qualified here before analysis.
This namespace is not an authenticity guarantee or an executable adapter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from agentloop.interoperability.validation import ImportValidationError, validate_json_tree

EXTERNAL_KEY = "agentloop.external"


def read_external(trace: Any) -> dict[str, Any] | None:
    metadata = getattr(trace, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if "agentloop.harbor_trial" in metadata:
        from agentloop.integrations.harbor.trial_evidence import read_trial

        trial = read_trial(trace)
        measurement = trial["measurement"]
        return {
            "schema_version": "1.0",
            "source": {"system": "harbor", "format": "harbor_trial"},
            "receipt_id": trial["receipt_id"],
            "runtime_ms": measurement["runtime_ms"],
            "event_timing_complete": measurement["runtime_ms"] is not None,
            "execution_status": trial["outcome"]["execution_status"],
            "usage_complete": measurement["input_tokens"] is not None
            and measurement["output_tokens"] is not None,
            "reported_model_call_count": None,
            "comparison_eligible": True,
        }
    if EXTERNAL_KEY not in metadata:
        if metadata.get("external_evidence_schema") == "1.0" or any(
            isinstance(event.metadata, Mapping)
            and event.metadata.get("external_evidence_schema") == "1.0"
            for event in trace.events
        ):
            raise ImportValidationError(
                "invalid_external_evidence", EXTERNAL_KEY, "source event qualification is missing"
            )
        return None
    value = metadata[EXTERNAL_KEY]
    validate_json_tree(value)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "source",
        "receipt_id",
        "runtime_ms",
        "event_timing_complete",
        "execution_status",
        "usage_complete",
        "reported_model_call_count",
        "comparison_eligible",
    }:
        raise ImportValidationError(
            "invalid_external_evidence", EXTERNAL_KEY, "invalid measurement qualification"
        )
    if value["schema_version"] != "1.0" or not isinstance(value["source"], dict):
        raise ImportValidationError(
            "invalid_external_evidence", EXTERNAL_KEY, "unsupported qualification schema"
        )
    if not isinstance(value["receipt_id"], str) or not value["receipt_id"].startswith("receipt_"):
        raise ImportValidationError(
            "invalid_external_evidence", EXTERNAL_KEY, "missing source receipt reference"
        )
    for key in ("event_timing_complete", "usage_complete", "comparison_eligible"):
        if type(value[key]) is not bool:
            raise ImportValidationError(
                "invalid_external_evidence", EXTERNAL_KEY, "qualification flags must be boolean"
            )
    runtime = value["runtime_ms"]
    if runtime is not None and (
        type(runtime) not in {int, float} or not math.isfinite(runtime) or runtime < 0
    ):
        raise ImportValidationError(
            "invalid_external_evidence",
            EXTERNAL_KEY,
            "runtime must be finite and nonnegative or null",
        )
    count = value["reported_model_call_count"]
    if count is not None and (type(count) is not int or count < 0):
        raise ImportValidationError(
            "invalid_external_evidence", EXTERNAL_KEY, "invalid reported call count"
        )
    if not isinstance(value["execution_status"], str) or value["execution_status"] not in {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "partial",
        "missing",
        "unknown",
    }:
        raise ImportValidationError(
            "invalid_external_evidence", EXTERNAL_KEY, "invalid execution status"
        )
    if value["event_timing_complete"] and any(
        event.metadata.get("timing_available") is not True for event in trace.events
    ):
        raise ImportValidationError(
            "invalid_external_evidence",
            EXTERNAL_KEY,
            "event timing qualification contradicts events",
        )
    return value


def require_external_comparison(trace: Any, operation: str) -> None:
    if "agentloop.harbor_trial" in getattr(trace, "metadata", {}):
        from agentloop.integrations.harbor.trial_evidence import read_trial

        trial = read_trial(trace)
        # Studies already support missing per-metric values. A captured trial
        # record is complete evidence even when a measurement is unavailable.
        if operation == "study":
            return
        if trial["measurement"]["runtime_ms"] is None:
            raise ValueError(
                f"{operation} requires an externally recorded agent execution interval"
            )
        if operation == "value estimates":
            raise ValueError(
                "value estimates require finding-level timing; a trial phase is not a model/tool profile"
            )
        return
    evidence = read_external(trace)
    if evidence is not None and (
        not evidence["comparison_eligible"]
        or evidence["runtime_ms"] is None
        or evidence["execution_status"] == "unknown"
        or not evidence["event_timing_complete"]
        or not evidence["usage_complete"]
    ):
        raise ValueError(
            f"{operation} requires complete external trial measurements and independent quality evidence"
        )


def qualify_report(trace: Any, report: dict[str, Any]) -> None:
    evidence = read_external(trace)
    if evidence is None:
        return
    report["external_evidence"] = evidence
    report["total_runtime_ms"] = evidence["runtime_ms"]
    report["timing_status"] = "complete" if evidence["event_timing_complete"] else "unavailable"
    report["reported_model_call_count"] = evidence["reported_model_call_count"]
    report["model_call_count_basis"] = "recorded blocks; multiplicity is external reported"
    report["repeated_context_tokens"] = report["repeated_context_ratio"] = None
    report["retry_count"] = None
    report["rule_abstentions"] = [
        {
            "rule_id": "cache_context",
            "reason": "ATIF does not provide the actual provider input context"
            if evidence["source"].get("format") == "atif"
            else "Imported telemetry does not establish the complete provider input context",
        },
    ]
    models = [event for event in trace.events if event.event_type == "model_call"]
    for direction in ("input", "output"):
        key = direction + "_tokens"
        report["known_" + key] = report[key]
        if any(event.metadata.get(key + "_available") is not True for event in models) or (
            not models and not evidence["usage_complete"]
        ):
            report[key] = None
    for item in report["events"]:
        if item["metadata"].get("timing_available") is False:
            item["duration_ms"] = None
        if item["metadata"].get("source_status_available") is False:
            item["status"] = "unknown"
        if item["event_type"] == "model_call":
            for key in ("input_tokens", "output_tokens"):
                if item["metadata"].get(key + "_available") is False:
                    item[key] = None
    if not evidence["event_timing_complete"]:
        report["parallelism_opportunities"] = []
        for key in ("cumulative_span_time_ms", "model_time_ms", "tool_time_ms", "retry_time_ms"):
            report[key] = None
    report["token_status"] = "external_reported" if evidence["usage_complete"] else "partial"
    # Retained source cost lives in receipts. It is not provider accounting or a
    # rate calculated from independently verified provider usage.
    report["cost_status"] = "unknown"
    report["estimated_cost_usd"] = None


def qualify_candidates(report: dict[str, Any], candidates: list[Any]) -> None:
    evidence = report.get("external_evidence")
    if evidence is None:
        return
    for candidate in candidates:
        candidate.estimated_cost_savings_usd = None
        if not evidence["event_timing_complete"]:
            candidate.estimated_latency_savings_ms = None
            if candidate.estimate:
                inputs = candidate.estimate.get("inputs", {})
                inputs["sum_duration_ms"] = inputs["max_duration_ms"] = None
                candidate.estimate["unmodeled_metrics"] = ["latency", "cost"]
        candidate.observations = {
            **(candidate.observations or {}),
            "external_source": evidence["source"],
            "source_receipt_id": evidence["receipt_id"],
            "source_execution_status": evidence["execution_status"],
        }
        candidate.evidence_level = "external_reported"
        candidate.assumptions = [
            *(candidate.assumptions or []),
            "Source artifacts are externally reported; execution completeness and task correctness require independent verification.",
        ]
