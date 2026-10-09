"""Receipt-only cohort observations with independent acceptance conditions."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from agentloop.integrations.harbor.trial_evidence import PAIRING_KEYS, read_trial
from agentloop.interoperability.validation import ImportValidationError, validate_json_tree
from agentloop.interventions import canonical_json

COHORT_KEY = "agentloop.external_cohort"
COHORT_PAIRING_KEYS = (*PAIRING_KEYS, "task_id", "verifier_config_hash")


def _fail(reason: str) -> None:
    raise ImportValidationError("invalid_cohort_evidence", COHORT_KEY, reason)


def acceptance(
    outcome: dict,
    *,
    import_status: str,
    trajectory_available: bool,
    usage_available: bool,
    missingness_policy: str,
) -> bool | None:
    """Keep recorded correctness separate from usable completed-run evidence."""
    if (
        outcome["execution_status"] in {"failed", "timed_out", "cancelled"}
        or outcome["quality_pass"] is False
    ):
        return False
    complete = (
        import_status == "imported"
        and trajectory_available
        and usage_available
        and outcome["execution_status"] == "completed"
        and outcome["verifier_status"] == "available"
    )
    if not complete or outcome["quality_pass"] is None:
        return False if missingness_policy == "reject" else None
    return True


def cohort_record(
    *,
    source_receipt: dict | None,
    import_status: str,
    trajectory_available: bool,
    usage_available: bool,
    missingness_policy: str,
    condition: str,
    task_id: str | None,
    verifier_config_hash: str | None,
) -> dict:
    value = {
        "schema_version": "1.0",
        "source_receipt_id": source_receipt["receipt_id"] if source_receipt else None,
        "source_receipt_sha256": sha256(canonical_json(source_receipt).encode()).hexdigest()
        if source_receipt
        else None,
        "import_status": import_status,
        "trajectory_available": trajectory_available,
        "usage_available": usage_available,
        "missingness_policy": missingness_policy,
        "condition": condition,
        "task_id": task_id,
        "verifier_config_hash": verifier_config_hash,
    }
    value["sha256"] = sha256(canonical_json(value).encode()).hexdigest()
    return value


def read_cohort(trace: Any) -> dict | None:
    if COHORT_KEY not in trace.metadata:
        return None
    value = trace.metadata[COHORT_KEY]
    validate_json_tree(value)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "source_receipt_id",
            "source_receipt_sha256",
            "import_status",
            "trajectory_available",
            "usage_available",
            "missingness_policy",
            "condition",
            "task_id",
            "verifier_config_hash",
            "sha256",
        }
        or value["schema_version"] != "1.0"
    ):
        _fail("unsupported cohort observation schema")
    if (
        value["sha256"]
        != sha256(
            canonical_json({key: item for key, item in value.items() if key != "sha256"}).encode()
        ).hexdigest()
    ):
        _fail("cohort record changed after capture")
    if (
        type(value["trajectory_available"]) is not bool
        or type(value["usage_available"]) is not bool
        or not isinstance(value["missingness_policy"], str)
        or value["missingness_policy"] not in {"indeterminate", "reject"}
    ):
        _fail("invalid source coverage/missingness flags")
    if not isinstance(value["import_status"], str) or value["import_status"] not in {
        "imported",
        "invalid",
        "missing",
        "missing_trajectory",
    }:
        _fail("unsupported import status")
    for key in ("condition", "task_id", "source_receipt_id"):
        item = value[key]
        if item is None and key != "condition":
            continue
        if not isinstance(item, str) or not item.strip() or len(item.encode()) > 512:
            _fail("invalid cohort identity label")
    for key in ("source_receipt_sha256", "verifier_config_hash"):
        item = value[key]
        if item is not None and (
            not isinstance(item, str)
            or len(item) != 64
            or any(char not in "0123456789abcdef" for char in item)
        ):
            _fail("source/configuration hash must be SHA-256 or null")
    if (value["source_receipt_id"] is None) != (value["source_receipt_sha256"] is None):
        _fail("source receipt identity and digest must be present together")
    if any(
        trace.metadata.get(key) != value[key]
        for key in ("task_id", "verifier_config_hash", "condition")
    ):
        _fail("source pairing labels differ from their captured record")
    trial = read_trial(trace)
    if trial is None:
        _fail("cohort acceptance requires captured external trial evidence")
    if value["source_receipt_id"] is not None and value["source_receipt_id"] != trial["receipt_id"]:
        _fail("cohort receipt reference differs from captured trial source")
    result = json.loads(canonical_json(value))
    result["accepted"] = acceptance(
        trial["outcome"],
        import_status=value["import_status"],
        trajectory_available=value["trajectory_available"],
        usage_available=value["usage_available"],
        missingness_policy=value["missingness_policy"],
    )
    return result


def require_cohort_pair(baseline: Any, candidate: Any) -> None:
    left, right = read_cohort(baseline), read_cohort(candidate)
    if left is None and right is None:
        return
    if left is None or right is None:
        _fail("a cohort comparison requires two cohort observations")
    for key in ("task_id", "verifier_config_hash"):
        if baseline.metadata.get(key) is None or baseline.metadata.get(
            key
        ) != candidate.metadata.get(key):
            _fail("task/verifier identity is missing or incompatible")
    if any(
        item["import_status"] != "imported"
        or not item["trajectory_available"]
        or not item["usage_available"]
        for item in (left, right)
    ):
        _fail("cohort source coverage is incomplete; use the full-population study report")
