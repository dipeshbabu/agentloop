"""Snapshot-bound Omnigent observations, separate from enforcement and quality."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from agentloop.interoperability.validation import ImportValidationError, validate_json_tree
from agentloop.interventions import canonical_json

OMNIGENT_KEY = "agentloop.omnigent"


def _binding(trace: Any) -> str:
    value = trace.to_dict()
    value["metadata"] = {
        key: item for key, item in value["metadata"].items() if key != OMNIGENT_KEY
    }
    return sha256(canonical_json(value).encode()).hexdigest()


def attach_observations(trace: Any, observations: dict) -> None:
    """Bind one conservative source summary to the captured native projection."""
    if OMNIGENT_KEY in trace.metadata:
        raise ImportValidationError(
            "invalid_omnigent_evidence",
            OMNIGENT_KEY,
            "observations cannot replace an existing snapshot",
        )
    record = {
        "schema_version": "1.0",
        "run_id": trace.run_id,
        "trace_binding_sha256": _binding(trace),
        "observations": observations,
    }
    record["sha256"] = sha256(canonical_json(record).encode()).hexdigest()
    trace.metadata[OMNIGENT_KEY] = json.loads(canonical_json(record))


def read_observations(trace: Any) -> dict | None:
    record = trace.metadata.get(OMNIGENT_KEY)
    if record is None:
        return None
    validate_json_tree(record)
    if (
        not isinstance(record, dict)
        or set(record)
        != {"schema_version", "run_id", "trace_binding_sha256", "observations", "sha256"}
        or record["schema_version"] != "1.0"
    ):
        raise ImportValidationError(
            "invalid_omnigent_evidence", OMNIGENT_KEY, "unsupported observation snapshot"
        )
    observations = record["observations"]
    if (
        not isinstance(observations, dict)
        or observations.get("schema_version") != "1.0"
        or observations.get("enforcement") != "unverified"
        or observations.get("task_correctness") != "unavailable"
        or observations.get("source_execution_outcome") != "unknown"
        or observations.get("model_coverage") not in {"unknown", "observed_partial"}
    ):
        raise ImportValidationError(
            "invalid_omnigent_evidence", OMNIGENT_KEY, "unsupported observation claims"
        )
    if not isinstance(observations.get("policy_decisions"), list) or any(
        not isinstance(item, dict)
        or item.get("enforcement") != "unverified"
        or item.get("dispatch_prevention") != "unknown"
        for item in observations["policy_decisions"]
    ):
        raise ImportValidationError(
            "invalid_omnigent_evidence", OMNIGENT_KEY, "imported decisions cannot prove enforcement"
        )
    body = {key: value for key, value in record.items() if key != "sha256"}
    if (
        record["run_id"] != trace.run_id
        or record["trace_binding_sha256"] != _binding(trace)
        or record["sha256"] != sha256(canonical_json(body).encode()).hexdigest()
    ):
        raise ImportValidationError(
            "invalid_omnigent_evidence",
            OMNIGENT_KEY,
            "observations no longer match the captured projection",
        )
    return json.loads(canonical_json(record["observations"]))


def qualify_report(trace: Any, report: dict) -> None:
    observations = read_observations(trace)
    if observations is not None:
        report["omnigent_observations"] = observations
