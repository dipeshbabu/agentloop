"""Internal source receipt 1.0; execution measurements remain native traces."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from agentloop.interoperability.validation import (
    ImportLimits,
    ImportValidationError,
    relative_reference,
    validate_json_tree,
)
from agentloop.interventions import canonical_json

RECEIPT_SCHEMA_VERSION = "1.0"
SOURCE_SYSTEMS = frozenset({"harbor", "omnigent", "otel"})
PROVENANCE = frozenset(
    {
        "native_observed",
        "provider_reported",
        "external_reported",
        "calculated",
        "inferred",
        "synthetic",
        "unknown",
    }
)
IDENTITY_FIELDS = frozenset(
    {
        "job_id",
        "trial_id",
        "task_id",
        "task_digest",
        "step_id",
        "session_id",
        "trajectory_id",
        "trace_id",
    }
)
_COMPLETENESS_FIELDS = frozenset({"trajectory", "usage", "quality", "parentage", "timing", "cost"})
_COVERAGE = frozenset({"complete", "partial", "missing", "unknown", "not_applicable"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _error(field: str, reason: str) -> None:
    raise ImportValidationError("invalid_receipt", field, reason)


def _object(value: Any, field: str, expected: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _error(field, "must contain exactly the documented fields")
    return value


def _string(value: Any, field: str, *, nullable: bool = False, limit: int = 512) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        _error(field, "must be a nonempty bounded string")


def _enum(value: Any, field: str, choices: set[str] | frozenset[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        _error(field, "unsupported value")


def _hash(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _error(field, "must be a lowercase SHA-256 digest")


def external_id(kind: str, system: str, identity: dict[str, Any]) -> str:
    """Hash a typed identity tuple; callers explicitly supply fallback hashes.

    Do not use session IDs alone for document identity. Missing identity inputs
    remain missing; deriving a document key from artifact bytes is a caller
    decision whose provenance must be recorded in the receipt.
    """

    if kind not in {"run", "event", "receipt", "group"} or system not in SOURCE_SYSTEMS:
        raise ImportValidationError("invalid_identity", "identity", "unsupported namespace")
    validate_json_tree(identity)
    if not isinstance(identity, dict) or not identity:
        raise ImportValidationError("invalid_identity", "identity", "identity inputs are required")
    parts = []
    for key, value in sorted(identity.items()):
        _string(key, "identity.key")
        if value is None:
            continue
        if type(value) not in {str, int}:
            raise ImportValidationError(
                "invalid_identity", "identity", "identity values must be strings or integers"
            )
        if isinstance(value, str):
            _string(value, "identity.value", limit=4096)
        parts.append([key, type(value).__name__, value])
    if not parts:
        raise ImportValidationError(
            "invalid_identity", "identity", "non-null identity inputs are required"
        )
    digest = sha256(
        canonical_json({"system": system, "identity": parts}).encode("utf-8")
    ).hexdigest()
    return f"{kind}_{digest}"


def receipt_id(payload: dict[str, Any]) -> str:
    source = payload["source"]
    return external_id(
        "receipt",
        source["system"],
        {
            **payload["external_identity"],
            "artifact_reference": source["artifact_reference"],
            "artifact_sha256": source["artifact_sha256"],
            "source_format": source["format"],
            "format_version": source["format_version"],
        },
    )


def _validate_quality(outcome: dict[str, Any]) -> None:
    _object(
        outcome,
        "outcome",
        {
            "execution_status",
            "verifier_status",
            "verifier_dimensions",
            "quality_pass",
            "quality_basis",
            "scoring_contract",
            "verifier_isolation",
        },
    )
    _enum(
        outcome["execution_status"],
        "outcome.execution_status",
        {"completed", "failed", "timed_out", "cancelled", "partial", "missing", "unknown"},
    )
    _enum(
        outcome["verifier_status"],
        "outcome.verifier_status",
        {"available", "failed", "missing", "invalid", "unknown"},
    )
    _enum(
        outcome["verifier_isolation"],
        "outcome.verifier_isolation",
        {"shared", "separate", "mixed", "unknown"},
    )
    _enum(
        outcome["quality_basis"],
        "outcome.quality_basis",
        {"external_uninterpreted_reward", "configured_thresholds", "unavailable", "unknown"},
    )
    dimensions = outcome["verifier_dimensions"]
    if not isinstance(dimensions, dict):
        _error("outcome.verifier_dimensions", "must be an object")
    for name, value in dimensions.items():
        _string(name, "outcome.verifier_dimensions.key")
        if value is not None and type(value) not in {int, float}:
            _error("outcome.verifier_dimensions", "dimensions must be finite numbers or null")
    passed = outcome["quality_pass"]
    if passed is not None and type(passed) is not bool:
        _error("outcome.quality_pass", "must be a boolean or null")
    scoring = outcome["scoring_contract"]
    if scoring is None:
        if passed is not None or outcome["quality_basis"] == "configured_thresholds":
            _error("outcome.quality_pass", "a pass decision requires an explicit scoring contract")
        if outcome["quality_basis"] == "external_uninterpreted_reward" and (
            outcome["verifier_status"] != "available" or not dimensions
        ):
            _error("outcome.quality_basis", "uninterpreted rewards require available dimensions")
        return
    _object(
        scoring,
        "outcome.scoring_contract",
        {"schema_version", "scorer_id", "rule", "thresholds", "config_sha256"},
    )
    if scoring["schema_version"] != "1.0" or scoring["rule"] != "all_gte":
        _error("outcome.scoring_contract", "unsupported scoring contract")
    _string(scoring["scorer_id"], "outcome.scoring_contract.scorer_id")
    thresholds = scoring["thresholds"]
    if not isinstance(thresholds, dict) or not thresholds:
        _error("outcome.scoring_contract.thresholds", "explicit required dimensions are necessary")
    for name, value in thresholds.items():
        _string(name, "outcome.scoring_contract.thresholds.key")
        if type(value) not in {int, float}:
            _error("outcome.scoring_contract.thresholds", "thresholds must be finite numbers")
    config = {key: value for key, value in scoring.items() if key != "config_sha256"}
    expected_hash = sha256(canonical_json(config).encode("utf-8")).hexdigest()
    if scoring["config_sha256"] != expected_hash:
        _error("outcome.scoring_contract.config_sha256", "does not match the scoring definition")
    expected = None
    if outcome["verifier_status"] == "available" and all(
        dimensions.get(key) is not None for key in thresholds
    ):
        expected = all(dimensions[key] >= threshold for key, threshold in thresholds.items())
    if passed is not expected or outcome["quality_basis"] != "configured_thresholds":
        _error("outcome.quality_pass", "must match available rewards and configured thresholds")


def _validate_notice(notice: Any) -> None:
    _object(notice, "notices[]", {"code", "severity", "artifact", "field", "record"})
    if not isinstance(notice["code"], str) or not _CODE.fullmatch(notice["code"]):
        _error("notices[].code", "must be a bounded diagnostic code")
    _enum(notice["severity"], "notices[].severity", {"info", "warning", "error"})
    if notice["artifact"] is not None:
        relative_reference(notice["artifact"])
    _string(notice["field"], "notices[].field", nullable=True)
    if notice["record"] is not None and (type(notice["record"]) is not int or notice["record"] < 1):
        _error("notices[].record", "must be a positive source record number or null")


@dataclass(frozen=True)
class ImportReceipt:
    """Canonical JSON owns the snapshot; readers receive independent copies."""

    _json: str

    @property
    def receipt_id(self) -> str:
        return self.to_dict()["receipt_id"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, limits: ImportLimits = ImportLimits()
    ) -> ImportReceipt:
        validate_json_tree(payload, limits)
        _object(
            payload,
            "receipt",
            {
                "schema_version",
                "receipt_id",
                "source",
                "external_identity",
                "identity_provenance",
                "traces",
                "missing_trace_reason",
                "outcome",
                "completeness",
                "relationships",
                "notices",
                "source_metadata",
            },
        )
        if payload["schema_version"] != RECEIPT_SCHEMA_VERSION:
            _error("schema_version", "unsupported receipt version")
        source = _object(
            payload["source"],
            "source",
            {
                "system",
                "producer_version",
                "producer_revision",
                "format",
                "format_version",
                "artifact_reference",
                "artifact_sha256",
                "trust",
            },
        )
        _enum(source["system"], "source.system", SOURCE_SYSTEMS)
        formats = {
            "harbor": {"atif", "harbor_trial", "otlp_json", "otlp_jsonl"},
            "omnigent": {"otlp_json", "otlp_jsonl"},
            "otel": {"otlp_json", "otlp_jsonl"},
        }
        _enum(source["format"], "source.format", formats[source["system"]])
        for key in ("producer_version", "format_version"):
            _string(source[key], "source." + key, nullable=True)
        revision = source["producer_revision"]
        if revision is not None and (
            not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
        ):
            _error("source.producer_revision", "must be a pinned full Git SHA or null")
        relative_reference(source["artifact_reference"], limits)
        _hash(source["artifact_sha256"], "source.artifact_sha256")
        _enum(source["trust"], "source.trust", {"agent_writable", "external_reported", "unknown"})
        identity = _object(payload["external_identity"], "external_identity", IDENTITY_FIELDS)
        for key, value in identity.items():
            _string(value, "external_identity." + key, nullable=True)
        if identity["task_digest"] is not None:
            digest = identity["task_digest"]
            if not digest.startswith("sha256:"):
                _error("external_identity.task_digest", "must be a prefixed SHA-256 digest")
            _hash(digest[7:], "external_identity.task_digest")
        provenance = _object(payload["identity_provenance"], "identity_provenance", IDENTITY_FIELDS)
        for key, value in provenance.items():
            _enum(value, "identity_provenance." + key, PROVENANCE)
            if identity[key] is None and value != "unknown":
                _error("identity_provenance." + key, "absent identities have unknown provenance")
        if payload["receipt_id"] != receipt_id(payload):
            _error("receipt_id", "does not match source identity and supplied artifact bytes")
        traces = payload["traces"]
        if not isinstance(traces, list) or len(traces) > limits.max_trajectories:
            _error("traces", "must be a bounded list")
        seen_runs, seen_paths = set(), set()
        for item in traces:
            _object(item, "traces[]", {"run_id", "trace_file", "trace_sha256"})
            _string(item["run_id"], "traces[].run_id")
            relative_reference(item["trace_file"], limits)
            _hash(item["trace_sha256"], "traces[].trace_sha256")
            if item["run_id"] in seen_runs or item["trace_file"] in seen_paths:
                _error("traces", "duplicate native trace identity or reference")
            seen_runs.add(item["run_id"])
            seen_paths.add(item["trace_file"])
        reason = payload["missing_trace_reason"]
        if traces and reason is not None:
            _error("missing_trace_reason", "must be null when native traces are present")
        if not traces:
            _enum(
                reason,
                "missing_trace_reason",
                {
                    "missing_artifact",
                    "invalid_artifact",
                    "untimed_operations",
                    "no_execution_spans",
                    "unsupported_format",
                },
            )
        _validate_quality(payload["outcome"])
        completeness = _object(payload["completeness"], "completeness", _COMPLETENESS_FIELDS)
        for key, value in completeness.items():
            _enum(value, "completeness." + key, _COVERAGE)
        relationships = payload["relationships"]
        if not isinstance(relationships, list) or len(relationships) > limits.max_references:
            _error("relationships", "must be a bounded list")
        for item in relationships:
            _object(item, "relationships[]", {"kind", "target_id", "basis", "resolved"})
            _enum(
                item["kind"],
                "relationships[].kind",
                {"delegation", "continuation", "session", "span_link"},
            )
            _string(item["target_id"], "relationships[].target_id")
            _enum(item["basis"], "relationships[].basis", PROVENANCE)
            if type(item["resolved"]) is not bool:
                _error("relationships[].resolved", "must be a boolean")
        if not isinstance(payload["notices"], list) or len(payload["notices"]) > limits.max_nodes:
            _error("notices", "must be a bounded list")
        for notice in payload["notices"]:
            _validate_notice(notice)
        metadata = payload["source_metadata"]
        if (
            not isinstance(metadata, dict)
            or len(canonical_json(metadata).encode("utf-8")) > limits.max_metadata_bytes
        ):
            _error("source_metadata", "must be a bounded sanitized object")
        serialized = canonical_json(payload)
        if len(serialized.encode("utf-8")) > limits.max_json_bytes:
            _error("receipt", "receipt byte limit exceeded")
        return cls(serialized)


def summarize_receipts(receipts: list[ImportReceipt]) -> dict[str, Any]:
    """Receipt-level inventory, never extrapolated into trial or success counts."""

    seen = set()
    report: dict[str, Any] = {
        "receipts": 0,
        "native_traces": 0,
        "without_native_traces": 0,
        "verifier_available": 0,
        "external_quality_uninterpreted": 0,
        "notices_by_code": {},
        "source_versions": [],
    }
    report.update(dict.fromkeys(("quality_pass", "quality_fail", "quality_indeterminate"), 0))
    versions = set()
    for receipt in receipts:
        data = receipt.to_dict()
        if receipt.receipt_id in seen:
            _error("receipts", "duplicate receipt identity")
        seen.add(receipt.receipt_id)
        report["receipts"] += 1
        report["native_traces"] += len(data["traces"])
        report["without_native_traces"] += int(not data["traces"])
        outcome = data["outcome"]
        report["verifier_available"] += int(outcome["verifier_status"] == "available")
        report["external_quality_uninterpreted"] += int(
            outcome["quality_basis"] == "external_uninterpreted_reward"
        )
        key = (
            "quality_indeterminate"
            if outcome["quality_pass"] is None
            else "quality_pass"
            if outcome["quality_pass"]
            else "quality_fail"
        )
        report[key] += 1
        for notice in data["notices"]:
            codes = report["notices_by_code"]
            codes[notice["code"]] = codes.get(notice["code"], 0) + 1
        source = data["source"]
        versions.add(
            (
                source["system"],
                source["producer_version"],
                source["producer_revision"],
                source["format"],
                source["format_version"],
            )
        )
    report["notices_by_code"] = dict(sorted(report["notices_by_code"].items()))
    report["source_versions"] = [
        dict(
            zip(
                ("system", "producer_version", "producer_revision", "format", "format_version"),
                values,
            )
        )
        for values in sorted(versions, key=lambda values: tuple(value or "" for value in values))
    ]
    return report
