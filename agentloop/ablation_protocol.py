"""Frozen declarations for host-run harness ablations, without executing workloads."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

SCHEMA_VERSION = "1.0"
MODES = frozenset({"uninstrumented", "tracing", "shadow", "enforce"})
VERSION_KEYS = frozenset(
    {
        "source",
        "model",
        "provider",
        "configuration",
        "scorer",
        "runner",
        "environment",
        "tools",
        "reset",
    }
)


class AblationValidationError(ValueError):
    """A declaration or observation cannot be interpreted without guessing."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise AblationValidationError(
            "ablation artifacts must contain finite JSON values"
        ) from None


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise AblationValidationError(
            f"{label} must contain exactly: {', '.join(sorted(expected))}"
        )


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AblationValidationError(f"{label} must be nonempty text")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise AblationValidationError("timestamps must be ISO-8601 with a timezone") from None


def number(value, label, *, maximum=None, integer=False, nullable=False):
    if value is None and nullable:
        return
    valid = type(value) is int if integer else type(value) in {int, float}
    try:
        valid = (
            valid and math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)
        )
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise AblationValidationError(
            f"{label} must be a finite nonnegative {'integer' if integer else 'number'}"
        )


def pair_key(value):
    return value["task_id"], value["repetition"], value["cache_condition"]


def _validate(spec):
    fields(
        spec,
        {
            "schema_version",
            "name",
            "workload_id",
            "permission_ref",
            "frozen_at",
            "synthetic",
            "versions",
            "tasks",
            "conditions",
            "schedule",
            "quality_gate",
            "bootstrap",
        },
        "protocol",
    )
    if spec["schema_version"] != SCHEMA_VERSION or type(spec["synthetic"]) is not bool:
        raise AblationValidationError("protocol requires schema 1.0 and a boolean synthetic marker")
    for key in ("name", "workload_id", "permission_ref"):
        text_value(spec[key], key)
    timestamp(spec["frozen_at"])
    fields(spec["versions"], VERSION_KEYS, "versions")
    for key, value in spec["versions"].items():
        text_value(value, key)
    tasks = spec["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise AblationValidationError("tasks must be a nonempty mapping")
    digests = set()
    for task, details in tasks.items():
        text_value(task, "task_id")
        fields(details, {"split", "input_sha256"}, "task")
        if not isinstance(details["split"], str) or details["split"] not in {"pilot", "held_out"}:
            raise AblationValidationError("task split must be pilot or held_out")
        digest = details["input_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise AblationValidationError("task input_sha256 must be a SHA-256 hex digest")
        if digest in digests:
            raise AblationValidationError(
                "repeated task inputs must share one task ID, not inflate the task denominator"
            )
        digests.add(digest)
    if {item["split"] for item in tasks.values()} != {"pilot", "held_out"}:
        raise AblationValidationError("declare separate pilot and held_out tasks")
    conditions = spec["conditions"]
    if not isinstance(conditions, dict) or not conditions:
        raise AblationValidationError("conditions must be a mapping")
    signatures = set()
    for name, condition in conditions.items():
        text_value(name, "condition")
        fields(condition, {"mode", "policies"}, "condition")
        mode, policies = condition["mode"], condition["policies"]
        if not isinstance(mode, str) or mode not in MODES or not isinstance(policies, dict):
            raise AblationValidationError("invalid condition mode or policies")
        if bool(policies) != (mode in {"shadow", "enforce"}):
            raise AblationValidationError("only shadow/enforce conditions require policies")
        for policy, snapshot in policies.items():
            text_value(policy, "policy_id")
            text_value(snapshot, "policy version/configuration reference")
        signature = (mode, canonical(policies))
        if signature in signatures:
            raise AblationValidationError("duplicate mode/policy condition")
        signatures.add(signature)
    if {mode for mode, _ in signatures} != MODES:
        raise AblationValidationError(
            "declare uninstrumented, tracing, shadow and enforce conditions"
        )
    for condition in conditions.values():
        if condition["mode"] in {"shadow", "enforce"}:
            policies = condition["policies"]
            for mode in ("shadow", "enforce"):
                if (mode, canonical(policies)) not in signatures or any(
                    (mode, canonical({key: value})) not in signatures
                    for key, value in policies.items()
                ):
                    raise AblationValidationError(
                        "each policy set needs matched shadow/enforce and single-policy conditions"
                    )
    schedule = spec["schedule"]
    if not isinstance(schedule, list) or not schedule:
        raise AblationValidationError("schedule must be a nonempty list")
    seen = set()
    for slot in schedule:
        fields(slot, {"task_id", "repetition", "cache_condition", "order"}, "schedule slot")
        for key in ("task_id", "repetition", "cache_condition"):
            text_value(slot[key], key)
        if slot["task_id"] not in tasks or pair_key(slot) in seen:
            raise AblationValidationError(
                "schedule must reference declared tasks with unique pairing keys"
            )
        seen.add(pair_key(slot))
        order = slot["order"]
        if (
            not isinstance(order, list)
            or any(not isinstance(name, str) for name in order)
            or len(order) != len(conditions)
            or set(order) != set(conditions)
        ):
            raise AblationValidationError("every slot must order all conditions exactly once")
    if {slot["task_id"] for slot in schedule} != set(tasks):
        raise AblationValidationError("every declared task needs a scheduled slot")
    fields(spec["quality_gate"], {"min_score", "max_regression"}, "quality_gate")
    for key, value in spec["quality_gate"].items():
        number(value, key, maximum=1)
    fields(spec["bootstrap"], {"samples", "seed", "confidence"}, "bootstrap")
    settings = spec["bootstrap"]
    number(settings["samples"], "samples", maximum=10000, integer=True)
    if not settings["samples"] or type(settings["seed"]) is not int:
        raise AblationValidationError("bootstrap requires positive samples and an integer seed")
    number(settings["confidence"], "confidence", maximum=1)
    if not 0 < settings["confidence"] < 1:
        raise AblationValidationError("confidence must be strictly between zero and one")


@dataclass(frozen=True)
class AblationProtocol:
    """A canonical snapshot; its digest identifies declarations, not preregistration proof."""

    _json: str

    @classmethod
    def freeze(cls, specification):
        owned = json.loads(canonical(specification))
        _validate(owned)
        digest = sha256(canonical(owned).encode()).hexdigest()
        return cls(canonical({"protocol_hash": digest, "specification": owned}))

    @classmethod
    def from_dict(cls, payload):
        fields(payload, {"protocol_hash", "specification"}, "frozen protocol")
        result = cls.freeze(payload["specification"])
        if result.protocol_hash != payload["protocol_hash"]:
            raise AblationValidationError("protocol hash does not match the frozen specification")
        return result

    @property
    def protocol_hash(self):
        return self.to_dict()["protocol_hash"]

    def to_dict(self):
        return json.loads(self._json)
