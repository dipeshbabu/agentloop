"""Immutable evidence linking finding predictions to an observed replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agentloop.findings import build_diagnosis
from agentloop.harness_evidence import HarnessEvidenceError, comparison_evidence
from agentloop.replay import ReplayGates, build_replay_report

INTERVENTION_SCHEMA_VERSION = "1.0"
_IDENTITY_FIELDS = (
    "schema_version",
    "baseline_run_id",
    "candidate_run_id",
    "target_finding_ids",
    "intervention_type",
    "configuration",
)


class InterventionValidationError(ValueError):
    """An intervention does not satisfy the evidence contract."""


class InterventionReferenceError(LookupError):
    """A required trace or baseline finding is not available in this project."""


class InterventionConflictError(ValueError):
    """The same intervention identity already has different evidence."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InterventionValidationError("evidence must contain finite JSON values") from exc


def trace_fingerprint(trace: Any) -> str:
    return hashlib.sha256(canonical_json(trace.to_dict()).encode("utf-8")).hexdigest()


def _identity(payload: dict[str, Any]) -> str:
    identity = {key: payload[key] for key in _IDENTITY_FIELDS}
    return "int_" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InterventionRecord:
    """Canonical JSON owns the snapshot; callers receive independent copies."""

    _json: str

    @property
    def intervention_id(self) -> str:
        return self.to_dict()["intervention_id"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InterventionRecord:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != INTERVENTION_SCHEMA_VERSION
        ):
            raise InterventionValidationError("unsupported intervention schema_version")
        for key in ("baseline_run_id", "candidate_run_id", "intervention_type"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise InterventionValidationError(f"{key} must be a nonempty string")
        if payload["baseline_run_id"] == payload["candidate_run_id"]:
            raise InterventionValidationError("baseline and candidate must have distinct run IDs")
        targets = payload.get("target_finding_ids")
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(item, str) or not item for item in targets)
        ):
            raise InterventionValidationError("target_finding_ids must be a nonempty list of IDs")
        if targets != sorted(set(targets)):
            raise InterventionValidationError("target_finding_ids must be sorted and unique")
        for key in ("configuration", "metadata", "predicted", "measured", "trace_fingerprints"):
            if not isinstance(payload.get(key), dict):
                raise InterventionValidationError(f"{key} must be an object")
        findings = payload["predicted"].get("findings")
        if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
            raise InterventionValidationError("predicted.findings must contain finding snapshots")
        if [item.get("finding_id") for item in findings] != targets:
            raise InterventionValidationError("prediction snapshots must match target finding IDs")
        measured = payload["measured"]
        for condition in ("baseline", "candidate"):
            if (
                not isinstance(measured.get(condition), dict)
                or measured[condition].get("run_id") != payload[f"{condition}_run_id"]
            ):
                raise InterventionValidationError("replay run IDs must match the intervention")
            fingerprint = payload["trace_fingerprints"].get(condition)
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
            ):
                raise InterventionValidationError("trace fingerprints must be SHA-256 hex digests")
        if not isinstance(measured.get("deltas"), dict) or not isinstance(
            measured.get("gates"), dict
        ):
            raise InterventionValidationError("measured must retain replay deltas and gates")
        if (
            type(payload.get("gates_passed")) is not bool
            or measured["gates"].get("passed") is not payload["gates_passed"]
        ):
            raise InterventionValidationError("gates_passed must match the replay decision")
        if payload.get("intervention_id") != _identity(payload):
            raise InterventionValidationError("intervention_id does not match its identity fields")
        return cls(canonical_json(payload))


def build_intervention(
    baseline: Any,
    candidate: Any,
    *,
    target_finding_ids: list[str],
    intervention_type: str,
    configuration: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    diagnosis: dict[str, Any] | None = None,
    gates: ReplayGates | None = None,
    quality_report: dict[str, Any] | None = None,
    replay_report: dict[str, Any] | None = None,
) -> InterventionRecord:
    """Capture prediction snapshots, then attach the observed pair comparison."""
    if (
        not isinstance(target_finding_ids, list)
        or not target_finding_ids
        or any(not isinstance(item, str) or not item for item in target_finding_ids)
    ):
        raise InterventionValidationError("at least one target finding ID is required")
    diagnosis = build_diagnosis(baseline) if diagnosis is None else diagnosis
    if diagnosis.get("run_id") != baseline.run_id:
        raise InterventionValidationError("diagnosis must belong to the baseline run")
    targets = sorted(set(target_finding_ids))
    available = {finding["finding_id"]: finding for finding in diagnosis.get("findings", [])}
    if any(target not in available for target in targets):
        raise InterventionReferenceError("target baseline finding not found")
    replay = (
        replay_report
        if replay_report is not None
        else build_replay_report(
            baseline,
            candidate,
            gates=gates,
            quality_report=quality_report,
        )
    )
    if metadata is not None and not isinstance(metadata, dict):
        raise InterventionValidationError("metadata must be an object")
    record_metadata = dict(metadata or {})
    if "agentloop.harness_evidence" in record_metadata:
        raise InterventionValidationError(
            "agentloop.harness_evidence is derived from the compared traces"
        )
    try:
        harness_evidence = comparison_evidence(baseline, candidate)
    except HarnessEvidenceError:
        raise InterventionValidationError("invalid harness evidence in compared traces") from None
    if harness_evidence is not None:
        record_metadata["agentloop.harness_evidence"] = harness_evidence
    payload = {
        "schema_version": INTERVENTION_SCHEMA_VERSION,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "target_finding_ids": targets,
        "intervention_type": intervention_type,
        "configuration": {} if configuration is None else configuration,
        "predicted": {"findings": [available[target] for target in targets]},
        "measured": replay,
        "gates_passed": replay["gates"]["passed"],
        "trace_fingerprints": {
            "baseline": trace_fingerprint(baseline),
            "candidate": trace_fingerprint(candidate),
        },
        "metadata": record_metadata,
    }
    payload["intervention_id"] = _identity(payload)
    return InterventionRecord.from_dict(payload)
