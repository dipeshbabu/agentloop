"""Versioned, payload-free harness evidence carried by existing trace artifacts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from agentloop.budget_types import BudgetSnapshot, BudgetValidationError

EVIDENCE_VERSION = "1.1"
_IDENTITY_VERSION = "1.0"
_COMPARISON_VERSION = "1.0"
METADATA_KEY = "agentloop.harness"
_ACTIONS = {"continue", "deny", "stop", "escalate"}
_OUTCOMES = {"proposed", "applied", "rejected", "failed", "no_op"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_DECISION_ID = re.compile(r"hdec_[a-f0-9]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_FIELDS = {
    "schema_version",
    "decision_id",
    "hook_id",
    "run_id",
    "call_id",
    "branch_id",
    "trace_id",
    "span_id",
    "policy_id",
    "policy_version",
    "policy_config_hash",
    "harness_config_hash",
    "boundary",
    "phase",
    "mode",
    "requested_action",
    "resolved_action",
    "outcome",
    "reason_code",
    "dispatched",
    "execution_status",
    "evidence_refs",
    "conflicting_decision_ids",
    "retry_of",
    "timing",
    "budget_snapshot",
    "evaluation_status",
    "origin",
    "hook_sequence",
    "policy_order",
}
_CURRENT_FIELDS = _FIELDS | {"feedback"}


class HarnessEvidenceError(ValueError):
    """Harness evidence is malformed, unsupported, or conflicts with an earlier record."""


def canonical_json(value: Any) -> str:
    """Own a finite JSON snapshot without retaining caller-owned containers."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise HarnessEvidenceError("harness evidence must contain finite JSON values") from None


def hook_id(run_id: str, call_id: str, boundary: str, phase: str) -> str:
    identity = [_IDENTITY_VERSION, run_id, call_id, boundary, phase]
    return "hook_" + sha256(canonical_json(identity).encode()).hexdigest()


def decision_id(run_id: str, call_id: str, boundary: str, phase: str, policy_id: str) -> str:
    """Return one stable identity for a policy at a particular call boundary."""
    identity = [_IDENTITY_VERSION, run_id, call_id, boundary, phase, policy_id]
    return "hdec_" + sha256(canonical_json(identity).encode()).hexdigest()


def valid_decision_id(value: Any) -> bool:
    return isinstance(value, str) and _DECISION_ID.fullmatch(value) is not None


def empty_evidence() -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_VERSION,
        "policies": {},
        "decisions": {},
        "capture_errors": [],
    }


def _envelope(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "policies", "decisions", "capture_errors"}
        or not isinstance(value.get("schema_version"), str)
        or value.get("schema_version") not in {"1.0", EVIDENCE_VERSION}
        or not isinstance(value.get("policies"), dict)
        or not isinstance(value.get("decisions"), dict)
        or not isinstance(value.get("capture_errors"), list)
    ):
        raise HarnessEvidenceError("invalid or unsupported harness evidence envelope")
    return value


@dataclass(frozen=True)
class HarnessDecisionRecord:
    """Immutable decision evidence; exports are independent JSON copies."""

    _json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HarnessDecisionRecord:
        if not isinstance(value, dict):
            raise HarnessEvidenceError("invalid harness decision fields")
        version = value.get("schema_version")
        if not isinstance(version, str) or version not in {"1.0", EVIDENCE_VERSION}:
            raise HarnessEvidenceError("unsupported harness decision version")
        if set(value) != (_FIELDS if version == "1.0" else _CURRENT_FIELDS):
            raise HarnessEvidenceError("invalid harness decision fields")
        feedback = value.get("feedback")
        if feedback is not None and (
            not isinstance(feedback, str) or not feedback or len(feedback) > 256
        ):
            raise HarnessEvidenceError("invalid bounded decision feedback")
        for key in ("run_id", "call_id", "branch_id", "policy_id", "policy_version", "reason_code"):
            if not isinstance(value[key], str) or not value[key]:
                raise HarnessEvidenceError(
                    "decision identity and reason fields must be nonempty strings"
                )
        for key in ("policy_id", "policy_version", "reason_code"):
            if not _LABEL.fullmatch(value[key]):
                raise HarnessEvidenceError(
                    "policy identity and reasons must be bounded identifiers"
                )
        for key in ("trace_id", "span_id"):
            if value[key] is not None and (not isinstance(value[key], str) or not value[key]):
                raise HarnessEvidenceError("trace/span references must be strings or null")
        for key in ("policy_config_hash", "harness_config_hash"):
            if not isinstance(value[key], str) or not _HASH.fullmatch(value[key]):
                raise HarnessEvidenceError("configuration hashes must be SHA-256 hex digests")
        for key in (
            "boundary",
            "phase",
            "mode",
            "origin",
            "requested_action",
            "resolved_action",
            "outcome",
            "execution_status",
            "evaluation_status",
        ):
            if not isinstance(value[key], str):
                raise HarnessEvidenceError("decision enum fields must be strings")
        if value["boundary"] not in {"model", "tool", "iteration", "completion"} or value[
            "phase"
        ] not in {"before", "after"}:
            raise HarnessEvidenceError("unsupported decision boundary")
        if value["mode"] not in {"shadow", "enforce"} or value["origin"] not in {
            "policy",
            "harness",
        }:
            raise HarnessEvidenceError("unsupported decision mode or origin")
        if value["requested_action"] not in _ACTIONS or value["resolved_action"] not in _ACTIONS:
            raise HarnessEvidenceError("unsupported decision action")
        if value["outcome"] not in _OUTCOMES:
            raise HarnessEvidenceError("unsupported decision outcome")
        if value["mode"] == "shadow" and value["outcome"] not in {"proposed", "failed", "no_op"}:
            raise HarnessEvidenceError("shadow decisions cannot be applied or rejected controls")
        if value["outcome"] == "applied" and (
            value["requested_action"] == "continue"
            or value["requested_action"] != value["resolved_action"]
        ):
            raise HarnessEvidenceError("applied decisions must match the resolved control action")
        if type(value["dispatched"]) is not bool or (
            value["phase"] == "before" and value["dispatched"]
        ):
            raise HarnessEvidenceError("invalid dispatch evidence")
        if value["execution_status"] not in {
            "pending",
            "ok",
            "error",
            "cancelled",
            "closed",
            "denied",
        }:
            raise HarnessEvidenceError("unsupported execution status")
        if value["evaluation_status"] != "unverified":
            raise HarnessEvidenceError("a decision alone is not a measured evaluation")
        if value["hook_sequence"] is not None and (
            type(value["hook_sequence"]) is not int or value["hook_sequence"] < 0
        ):
            raise HarnessEvidenceError("hook sequence must be nonnegative or unavailable")
        if type(value["policy_order"]) is not int or value["policy_order"] < 0:
            raise HarnessEvidenceError("policy order must be nonnegative")
        if value["budget_snapshot"] is not None:
            try:
                budget = BudgetSnapshot.from_dict(value["budget_snapshot"]).to_dict()
                if value["mode"] == "shadow" and any(
                    budget[key] == "best_effort"
                    for key in ("spend_enforcement", "token_enforcement")
                ):
                    raise BudgetValidationError("shadow budgets cannot claim enforcement")
            except (BudgetValidationError, TypeError):
                raise HarnessEvidenceError("invalid or unsupported budget snapshot") from None
        for key in ("evidence_refs", "conflicting_decision_ids"):
            items = value[key]
            if (
                not isinstance(items, list)
                or any(not isinstance(item, str) or not item for item in items)
                or items != sorted(set(items))
            ):
                raise HarnessEvidenceError("decision references must be sorted unique IDs")
        refs = value["conflicting_decision_ids"] + (
            [value["retry_of"]] if value["retry_of"] is not None else []
        )
        if any(not valid_decision_id(item) or item == value["decision_id"] for item in refs):
            raise HarnessEvidenceError("invalid related decision identity")
        identity_args = (value["run_id"], value["call_id"], value["boundary"], value["phase"])
        if value["decision_id"] != decision_id(*identity_args, value["policy_id"]) or value[
            "hook_id"
        ] != hook_id(*identity_args):
            raise HarnessEvidenceError("decision identity does not match its source")
        timing = value["timing"]
        if not isinstance(timing, dict) or set(timing) != {
            "started_at",
            "duration_ms",
            "hook_duration_ms",
        }:
            raise HarnessEvidenceError("invalid decision timing")
        if timing["started_at"] is not None:
            try:
                parsed = datetime.fromisoformat(timing["started_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise HarnessEvidenceError("decision timestamp must include a timezone") from None
        for key in ("duration_ms", "hook_duration_ms"):
            item = timing[key]
            if item is not None:
                try:
                    valid = type(item) in {int, float} and math.isfinite(float(item)) and item >= 0
                except (OverflowError, ValueError):
                    valid = False
                if not valid:
                    raise HarnessEvidenceError("decision durations must be finite and nonnegative")
        return cls(canonical_json(value))


def policy_snapshot(policy: Any, *, include_configuration: bool) -> dict[str, Any]:
    """Capture declarations; raw configuration requires explicit caller opt-in."""

    # Policy's configuration is already deeply frozen and JSON-validated. Convert
    # its mapping/tuple containers only when the caller requests raw capture.
    def native(value):
        from collections.abc import Mapping

        if isinstance(value, Mapping):
            return {key: native(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [native(item) for item in value]
        return value

    return {
        "policy_id": policy.policy_id,
        "version": policy.version,
        "config_hash": policy.config_hash,
        "priority": policy.priority,
        "hooks": [
            {"boundary": hook.boundary, "phase": hook.phase} for hook in sorted(policy.hooks)
        ],
        "actions": sorted(policy.actions),
        "configuration_capture": "explicit" if include_configuration else "redacted",
        "configuration": native(policy.configuration) if include_configuration else None,
    }


def records_for_hook(
    result: Any, config_hash: str, system_snapshot: dict
) -> tuple[HarnessDecisionRecord, ...]:
    """Resolve each proposal separately while sharing one hook/effect identity."""
    identities = {
        item.policy_id: decision_id(
            result.run_id, result.call_id, result.hook.boundary, result.hook.phase, item.policy_id
        )
        for item in result.proposals
    }
    rows = result.proposals or (None,)
    records = []
    for policy_order, proposal in enumerate(rows):
        action = proposal.decision.action if proposal is not None else result.action
        if proposal is not None and proposal.failed:
            outcome = "failed"
        elif action == "continue":
            outcome = "no_op"
        elif result.mode == "shadow":
            outcome = "proposed"
        elif result.applied and action == result.action:
            outcome = "applied"
        else:
            outcome = "rejected"
        policy_id = proposal.policy_id if proposal is not None else system_snapshot["policy_id"]
        record_id = decision_id(
            result.run_id, result.call_id, result.hook.boundary, result.hook.phase, policy_id
        )
        records.append(
            HarnessDecisionRecord.from_dict(
                {
                    "schema_version": EVIDENCE_VERSION,
                    "decision_id": record_id,
                    "hook_id": hook_id(
                        result.run_id, result.call_id, result.hook.boundary, result.hook.phase
                    ),
                    "run_id": result.run_id,
                    "call_id": result.call_id,
                    "branch_id": result.branch_id,
                    "trace_id": result.trace_id,
                    "span_id": result.parent_span_id,
                    "policy_id": policy_id,
                    "policy_version": proposal.policy_version
                    if proposal is not None
                    else system_snapshot["version"],
                    "policy_config_hash": proposal.config_hash
                    if proposal is not None
                    else system_snapshot["config_hash"],
                    "harness_config_hash": config_hash,
                    "boundary": result.hook.boundary,
                    "phase": result.hook.phase,
                    "mode": result.mode,
                    "requested_action": action,
                    "resolved_action": result.action,
                    "outcome": outcome,
                    "reason_code": proposal.decision.reason_code
                    if proposal is not None
                    else "run_stopped"
                    if result.action == "stop"
                    else "no_policy_change",
                    "dispatched": result.dispatched,
                    "execution_status": result.status,
                    "evidence_refs": sorted(set(proposal.decision.evidence_refs))
                    if proposal is not None
                    else [],
                    "conflicting_decision_ids": sorted(
                        identities[item.policy_id]
                        for item in result.proposals
                        if item.policy_id != policy_id
                        and item.decision.action != action
                        and item.decision.action != "continue"
                        and action != "continue"
                    ),
                    "retry_of": proposal.decision.retry_of if proposal is not None else None,
                    "timing": {
                        "started_at": proposal.started_at
                        if proposal is not None
                        else result.started_at,
                        "duration_ms": proposal.duration_ms if proposal is not None else 0.0,
                        "hook_duration_ms": result.duration_ms,
                    },
                    "budget_snapshot": (
                        proposal.decision.budget_snapshot.to_dict()
                        if proposal is not None and proposal.decision.budget_snapshot is not None
                        else None
                    ),
                    "evaluation_status": "unverified",
                    "origin": "policy" if proposal is not None else "harness",
                    "feedback": proposal.decision.feedback if proposal is not None else None,
                    "hook_sequence": result.sequence,
                    "policy_order": policy_order,
                }
            )
        )
    return tuple(records)


def append_records(
    envelope: dict, records: tuple[HarnessDecisionRecord, ...], snapshots: dict[str, dict]
) -> None:
    """Append only new identities in O(new records); exact retries are idempotent."""
    target = _envelope(envelope)
    updates = []
    for record in records:
        payload = record.to_dict()
        identity, config_hash = payload["decision_id"], payload["policy_config_hash"]
        snapshot = snapshots.get(config_hash)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("config_hash") != config_hash
            or snapshot.get("policy_id") != payload["policy_id"]
            or snapshot.get("version") != payload["policy_version"]
        ):
            raise HarnessEvidenceError("decision policy snapshot is missing or incompatible")
        snapshot_json = canonical_json(snapshot)
        existing = target["decisions"].get(identity)
        if existing is not None and canonical_json(existing) != record._json:
            raise HarnessEvidenceError("decision identity already has different evidence")
        saved_policy = target["policies"].get(config_hash)
        if saved_policy is not None and canonical_json(saved_policy) != snapshot_json:
            raise HarnessEvidenceError("policy snapshot already has different evidence")
        updates.append((identity, config_hash, payload, snapshot_json))
    for identity, config_hash, payload, snapshot_json in updates:
        if config_hash not in target["policies"]:
            target["policies"].setdefault(config_hash, json.loads(snapshot_json))
        target["decisions"].setdefault(identity, payload)
        if payload["schema_version"] == EVIDENCE_VERSION:
            target["schema_version"] = EVIDENCE_VERSION


def validate_evidence(value: Any, *, trace_id: str | None = None) -> dict[str, Any]:
    """Validate a standalone artifact or evidence belonging to one native trace."""
    envelope = _envelope(value)
    snapshot_fields = {
        "policy_id",
        "version",
        "config_hash",
        "priority",
        "hooks",
        "actions",
        "configuration_capture",
        "configuration",
    }
    for config_hash, snapshot in envelope["policies"].items():
        if (
            not isinstance(config_hash, str)
            or not _HASH.fullmatch(config_hash)
            or not isinstance(snapshot, dict)
            or set(snapshot) != snapshot_fields
            or snapshot["config_hash"] != config_hash
        ):
            raise HarnessEvidenceError("invalid policy snapshot")
        if (
            any(
                not isinstance(snapshot[key], str) or not snapshot[key]
                for key in ("policy_id", "version", "configuration_capture")
            )
            or type(snapshot["priority"]) is not int
        ):
            raise HarnessEvidenceError("invalid policy declaration")
        if any(not _LABEL.fullmatch(snapshot[key]) for key in ("policy_id", "version")):
            raise HarnessEvidenceError("invalid policy identity")
        if snapshot["configuration_capture"] == "redacted":
            if snapshot["configuration"] is not None:
                raise HarnessEvidenceError("redacted policy configuration must be omitted")
        elif snapshot["configuration_capture"] != "explicit" or not isinstance(
            snapshot["configuration"], dict
        ):
            raise HarnessEvidenceError("invalid policy configuration capture")
        actions = snapshot["actions"]
        if (
            not isinstance(actions, list)
            or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
            or actions != sorted(set(actions))
        ):
            raise HarnessEvidenceError("invalid policy action declaration")
        if not isinstance(snapshot["hooks"], list):
            raise HarnessEvidenceError("invalid policy hook declaration")
        for hook in snapshot["hooks"]:
            if (
                not isinstance(hook, dict)
                or set(hook) != {"boundary", "phase"}
                or not isinstance(hook["boundary"], str)
                or not isinstance(hook["phase"], str)
                or hook["boundary"] not in {"model", "tool", "iteration", "completion"}
                or hook["phase"] not in {"before", "after"}
            ):
                raise HarnessEvidenceError("invalid policy hook declaration")
    hooks = {}
    orders: dict[str, set[int]] = {}
    for identity, payload in envelope["decisions"].items():
        record = HarnessDecisionRecord.from_dict(payload).to_dict()
        if envelope["schema_version"] == "1.0" and record["schema_version"] != "1.0":
            raise HarnessEvidenceError("decision version exceeds its envelope version")
        if identity != record["decision_id"] or (
            trace_id is not None and record["trace_id"] != trace_id
        ):
            raise HarnessEvidenceError("decision does not belong to this trace")
        snapshot = envelope["policies"].get(record["policy_config_hash"])
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("config_hash") != record["policy_config_hash"]
            or snapshot.get("policy_id") != record["policy_id"]
            or snapshot.get("version") != record["policy_version"]
        ):
            raise HarnessEvidenceError("decision policy snapshot is missing or incompatible")
        if record["outcome"] != "failed" and (
            record["requested_action"] not in snapshot["actions"]
            or {"boundary": record["boundary"], "phase": record["phase"]} not in snapshot["hooks"]
        ):
            raise HarnessEvidenceError("decision contradicts its policy declaration")
        hook_fields = {
            key: record[key]
            for key in (
                "run_id",
                "call_id",
                "branch_id",
                "trace_id",
                "span_id",
                "harness_config_hash",
                "boundary",
                "phase",
                "mode",
                "resolved_action",
                "dispatched",
                "execution_status",
                "hook_sequence",
            )
        }
        hook_fields["duration_ms"] = record["timing"]["hook_duration_ms"]
        if hooks.setdefault(record["hook_id"], hook_fields) != hook_fields:
            raise HarnessEvidenceError("decisions disagree about their shared hook")
        seen_orders = orders.setdefault(record["hook_id"], set())
        if record["policy_order"] in seen_orders:
            raise HarnessEvidenceError("duplicate policy order within a hook")
        seen_orders.add(record["policy_order"])
        for related in record["conflicting_decision_ids"]:
            other = envelope["decisions"].get(related)
            if isinstance(other, dict) and other.get("hook_id") != record["hook_id"]:
                raise HarnessEvidenceError("conflict references must belong to the same hook")
    for error in envelope["capture_errors"]:
        if (
            not isinstance(error, dict)
            or set(error) != {"call_id", "phase", "category"}
            or not isinstance(error["call_id"], str)
            or not isinstance(error["phase"], str)
            or error["phase"] not in {"before", "after"}
            or error["category"] != "trace_evidence_error"
        ):
            raise HarnessEvidenceError("invalid capture diagnostic")
    return json.loads(canonical_json(envelope))


def read_evidence(trace: Any) -> dict[str, Any] | None:
    """Validate/copy the known envelope without rewriting unknown native metadata."""
    if METADATA_KEY not in trace.metadata:
        return None
    return validate_evidence(trace.metadata[METADATA_KEY], trace_id=trace.run_id)


def comparison_evidence(baseline: Any, candidate: Any) -> dict[str, Any] | None:
    """Snapshot decision evidence only inside a real baseline/candidate comparison."""
    before, after = read_evidence(baseline), read_evidence(candidate)
    if before is None and after is None:
        return None
    applied = (
        []
        if after is None
        else sorted(
            identity
            for identity, record in after["decisions"].items()
            if record["mode"] == "enforce"
            and record["outcome"] == "applied"
            and record["origin"] == "policy"
        )
    )
    unresolved = sorted(
        {
            reference
            for envelope in (before, after)
            if envelope is not None
            for record in envelope["decisions"].values()
            for reference in record["conflicting_decision_ids"]
            + ([record["retry_of"]] if record["retry_of"] else [])
            if reference not in envelope["decisions"]
        }
    )
    return {
        "schema_version": _COMPARISON_VERSION,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "comparison_status": "observed_pair",
        "capture_status": "known_incomplete"
        if unresolved
        or any(envelope["capture_errors"] for envelope in (before, after) if envelope is not None)
        else "no_reported_gaps",
        "individual_policy_effect": "unverified",
        "unresolved_decision_ids": unresolved,
        "applied_candidate_decision_ids": applied,
        "baseline": before,
        "candidate": after,
    }
