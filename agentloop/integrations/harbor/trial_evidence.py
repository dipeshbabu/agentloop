"""Frozen trial measurements separate from ATIF model/tool projections."""

from __future__ import annotations

import json
import math
from hashlib import sha256
from typing import Any

from agentloop.interoperability.contracts import _validate_quality
from agentloop.interoperability.validation import (
    ImportValidationError,
    relative_reference,
    validate_json_tree,
)
from agentloop.interventions import canonical_json

TRIAL_KEY = "agentloop.harbor_trial"
PAIRING_KEYS = (
    "task_digest",
    "scorer_config_hash",
    "environment_hash",
    "protocol_id",
    "repetition",
)


def _fail(reason: str) -> None:
    raise ImportValidationError("invalid_trial_evidence", TRIAL_KEY, reason)


def _binding(trace: Any) -> str:
    value = trace.to_dict()
    value["metadata"] = {key: item for key, item in value["metadata"].items() if key != TRIAL_KEY}
    return sha256(canonical_json(value).encode()).hexdigest()


def attach_trial(trace: Any, record: dict) -> None:
    if TRIAL_KEY in trace.metadata:
        _fail("trial evidence cannot replace an existing receipt")
    snapshot = {
        **record,
        "schema_version": "1.0",
        "run_id": trace.run_id,
        "trace_binding_sha256": _binding(trace),
    }
    snapshot["sha256"] = sha256(canonical_json(snapshot).encode()).hexdigest()
    trace.metadata[TRIAL_KEY] = json.loads(canonical_json(snapshot))
    read_trial(trace)


def read_trial(trace: Any) -> dict | None:
    if TRIAL_KEY not in trace.metadata:
        return None
    value = trace.metadata[TRIAL_KEY]
    validate_json_tree(value)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "run_id",
            "receipt_id",
            "source_result",
            "measurement",
            "outcome",
            "pairing",
            "trace_binding_sha256",
            "sha256",
        }
        or value["schema_version"] != "1.0"
    ):
        _fail("unsupported trial measurement schema")
    content = {key: item for key, item in value.items() if key != "sha256"}
    if (
        value["sha256"] != sha256(canonical_json(content).encode()).hexdigest()
        or value["trace_binding_sha256"] != _binding(trace)
        or value["run_id"] != trace.run_id
    ):
        _fail("trial measurements no longer match their captured trace")
    _validate_quality(value["outcome"])
    source = value["source_result"]
    if not isinstance(source, dict) or set(source) != {"reference", "sha256"}:
        _fail("invalid source result reference")
    relative_reference(source["reference"])
    if (
        not isinstance(source["sha256"], str)
        or len(source["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in source["sha256"])
    ):
        _fail("source result hash must be SHA-256")
    if not isinstance(value["receipt_id"], str) or not value["receipt_id"].startswith("receipt_"):
        _fail("source receipt reference is missing")
    measurement = value["measurement"]
    if not isinstance(measurement, dict) or set(measurement) != {
        "runtime_ms",
        "scope",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_usd",
        "usage_provenance",
        "cost_scope",
    }:
        _fail("invalid measurement fields")
    if (
        measurement["scope"] != "agent_execution"
        or measurement["usage_provenance"] != "external_reported"
        or measurement["cost_scope"] != "agent_context"
    ):
        _fail("unsupported measurement scope/provenance")
    for key in ("runtime_ms", "cost_usd"):
        number = measurement[key]
        if number is not None and (
            type(number) not in {int, float} or not math.isfinite(number) or number < 0
        ):
            _fail("measurement must be finite nonnegative or null")
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        number = measurement[key]
        if number is not None and (type(number) is not int or number < 0):
            _fail("usage must be a nonnegative integer or null")
    prompt, cache = measurement["input_tokens"], measurement["cached_input_tokens"]
    if prompt is not None and cache is not None and cache > prompt:
        _fail("cache usage is not a prompt subset")
    if not isinstance(value["pairing"], dict) or set(value["pairing"]) != set(PAIRING_KEYS):
        _fail("invalid pairing metadata")
    for key, item in value["pairing"].items():
        if trace.metadata.get(key) != item:
            _fail("pairing metadata differs from its captured source")
        if item is not None and type(item) not in {str, int, float, bool}:
            _fail("pairing keys must be scalar or absent")
    return json.loads(canonical_json(value))


def qualify_trial_report(trace: Any, report: dict) -> None:
    evidence = read_trial(trace)
    if evidence is None:
        return
    measurement = evidence["measurement"]
    report["external_trial"] = evidence
    report["total_runtime_ms"] = measurement["runtime_ms"]
    report["input_tokens"], report["output_tokens"] = (
        measurement["input_tokens"],
        measurement["output_tokens"],
    )
    report["token_status"] = (
        "external_reported"
        if measurement["input_tokens"] is not None and measurement["output_tokens"] is not None
        else "partial"
    )
    report["estimated_cost_usd"] = measurement["cost_usd"]
    report["cost_status"] = "complete" if measurement["cost_usd"] is not None else "unknown"
    report["cost_measurement_basis"] = "external_reported_agent_context"
    report["timing_status"] = "complete" if measurement["runtime_ms"] is not None else "unavailable"
    report["model_call_count_basis"] = "no model inference is manufactured from a trial phase"
    for key in (
        "model_call_count",
        "tool_call_count",
        "model_time_ms",
        "tool_time_ms",
        "retry_time_ms",
    ):
        report[key] = None
    report["repeated_context_tokens"] = report["repeated_context_ratio"] = None
    report["retry_count"] = None
    if measurement["runtime_ms"] is None:
        for key in ("cumulative_span_time_ms", "model_time_ms", "tool_time_ms", "retry_time_ms"):
            report[key] = None


def trial_study_values(evidence: dict) -> tuple[float | None, bool | None]:
    outcome = evidence["outcome"]
    status = outcome["execution_status"]
    passed = outcome["quality_pass"]
    success = (
        False
        if status in {"failed", "timed_out", "cancelled"} or passed is False
        else passed
        if status == "completed"
        else None
    )
    # This is a configured binary acceptance indicator, not a normalized reward.
    score = float(passed) if passed is not None and status == "completed" else None
    return score, success


def require_trial_pair(baseline: Any, candidate: Any) -> None:
    left, right = read_trial(baseline), read_trial(candidate)
    if left is None and right is None:
        return
    if left is None or right is None:
        _fail("external trial comparisons require two compatible trial records")
    for key in PAIRING_KEYS:
        a, b = left["pairing"][key], right["pairing"][key]
        if a is None or b is None or type(a) is not type(b) or a != b:
            _fail("task/scorer/environment/protocol/repetition identity is missing or incompatible")
