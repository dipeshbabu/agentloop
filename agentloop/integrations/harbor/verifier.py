"""External reward preservation and explicit, hashed acceptance criteria."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from agentloop.interoperability.contracts import _validate_quality
from agentloop.interoperability.validation import ImportValidationError, validate_json_tree
from agentloop.interventions import canonical_json


def _fail(field: str, reason: str) -> None:
    raise ImportValidationError("invalid_verifier", field, reason)


def reward_dimensions(value: Any) -> dict[str, int | float | None] | None:
    if value is None:
        return None
    validate_json_tree(value)
    if not isinstance(value, dict):
        _fail("rewards", "reward dimensions must be an object or null")
    for key, number in value.items():
        if not isinstance(key, str) or not key.strip() or len(key.encode()) > 512:
            _fail("rewards", "reward names must be bounded strings")
        if number is not None and (type(number) not in {int, float} or not math.isfinite(number)):
            _fail("rewards", "rewards must be finite numbers or null")
    return dict(value)


@dataclass(frozen=True)
class ScoringContract:
    _json: str

    def to_dict(self) -> dict:
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, value: dict) -> ScoringContract:
        validate_json_tree(value)
        if not isinstance(value, dict) or set(value) - {
            "schema_version",
            "scorer_id",
            "rule",
            "thresholds",
            "config_sha256",
        }:
            _fail("scoring", "unsupported scoring fields")
        required = {"schema_version", "scorer_id", "rule", "thresholds"}
        if not required <= set(value):
            _fail("scoring", "versioned scorer identity, rule and thresholds are required")
        config = {key: value[key] for key in required}
        digest = sha256(canonical_json(config).encode()).hexdigest()
        if value.get("config_sha256", digest) != digest:
            _fail("scoring.config_sha256", "scoring hash does not match its definition")
        config["config_sha256"] = digest
        # Reuse the frozen receipt contract, including boolean/nonfinite rejection.
        _validate_quality(
            {
                "execution_status": "unknown",
                "verifier_status": "missing",
                "verifier_dimensions": {},
                "quality_pass": None,  # nosec B105
                "quality_basis": "configured_thresholds",
                "scoring_contract": config,
                "verifier_isolation": "unknown",
            }
        )
        return cls(canonical_json(config))

    @classmethod
    def all_gte(cls, scorer_id: str, thresholds: dict[str, float]) -> ScoringContract:
        return cls.from_dict(
            {
                "schema_version": "1.0",
                "scorer_id": scorer_id,
                "rule": "all_gte",
                "thresholds": thresholds,
            }
        )


def external_outcome(
    *,
    execution_status: str,
    verifier_status: str,
    dimensions: dict | None,
    isolation: str = "unknown",
    scoring: ScoringContract | None = None,
) -> dict:
    rewards = reward_dimensions(dimensions)
    config = scoring.to_dict() if scoring is not None else None
    passed = None
    if (
        config is not None
        and verifier_status == "available"
        and rewards is not None
        and all(rewards.get(key) is not None for key in config["thresholds"])
    ):
        passed = all(rewards[key] >= threshold for key, threshold in config["thresholds"].items())
    result = {
        "execution_status": execution_status,
        "verifier_status": verifier_status,
        "verifier_dimensions": rewards if rewards is not None else {},
        "quality_pass": passed,
        "quality_basis": "configured_thresholds"
        if config is not None
        else "external_uninterpreted_reward"
        if verifier_status == "available" and rewards
        else "unavailable",
        "scoring_contract": config,
        "verifier_isolation": isolation,
    }
    _validate_quality(result)
    return result
