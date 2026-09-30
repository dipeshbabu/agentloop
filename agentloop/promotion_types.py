"""Frozen budget-policy promotion declarations and trusted operator attestations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from agentloop.ablation_protocol import AblationProtocol, number, timestamp
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_types import canonical, fingerprint, identifier


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(label + " requires a SHA-256 digest")


@dataclass(frozen=True)
class PromotionThresholds:
    min_held_out_tasks: int = 20
    min_held_out_pairs: int = 40
    min_calibration_tasks: int = 20
    max_age_days: float = 30
    min_quality_score: float = 0.95
    max_quality_regression: float = 0
    min_latency_improvement_ms: float = 1
    max_cost_regression_usd: float = 0
    max_shadow_overhead_ms: float = 10
    max_calibration_latency_mae_ms: float = 100
    max_calibration_cost_mae_usd: float = 0.01

    def __post_init__(self):
        for name, value in asdict(self).items():
            number(
                value,
                name,
                integer=name
                in {"min_held_out_tasks", "min_held_out_pairs", "min_calibration_tasks"},
            )
        if (
            min(self.min_held_out_tasks, self.min_calibration_tasks) < 20
            or self.min_held_out_pairs < self.min_held_out_tasks
        ):
            raise ValueError("promotion requires independent held-out tasks and complete pairs")
        if (
            not 0 < self.max_age_days <= 3650
            or self.min_latency_improvement_ms <= 0
            or max(self.min_quality_score, self.max_quality_regression) > 1
        ):
            raise ValueError("invalid promotion quality/time/performance threshold")


@dataclass(frozen=True)
class CanaryLimits:
    max_admissions: int
    max_duration_s: float
    max_latency_ms: float
    max_cost_usd: float
    min_quality_score: float = 0.95

    def __post_init__(self):
        for name, value in asdict(self).items():
            number(value, name, integer=name == "max_admissions")
        if (
            not 1 <= self.max_admissions <= 1000
            or not 0 < self.max_duration_s <= 86400
            or self.min_quality_score > 1
        ):
            raise ValueError("canary limits must be finite and bounded")


@dataclass(frozen=True)
class CapabilityEvidence:
    adapter: str
    source_version: str
    policy_config_hash: str
    artifact_hash: str
    tested_at: str
    passed: bool
    check_count: int

    def __post_init__(self):
        identifier(self.adapter, "adapter")
        if not isinstance(self.source_version, str) or not self.source_version:
            raise ValueError("capability evidence needs a source version")
        digest(self.policy_config_hash, "policy_config_hash")
        digest(self.artifact_hash, "artifact_hash")
        timestamp(self.tested_at)
        if (
            type(self.passed) is not bool
            or type(self.check_count) is not int
            or self.check_count < 0
        ):
            raise ValueError("capability evidence needs explicit results")


@dataclass(frozen=True, init=False)
class BudgetProposal:
    """Own all evidence; hashes bind a review, not evidence authenticity."""

    _json: str = field(repr=False)
    proposal_hash: str

    def __init__(
        self,
        proposal_id,
        *,
        baseline,
        candidate,
        protocol,
        observations,
        calibration,
        calibration_cohort,
        capability,
        thresholds=PromotionThresholds(),
        canary,
        valid_until,
        harness_evidence=None,
    ):
        identifier(proposal_id, "proposal_id")
        if type(baseline) is not BudgetLimits or type(candidate) is not BudgetLimits:
            raise ValueError("only built-in budget policy promotion is supported")
        if baseline != BudgetLimits():
            raise ValueError("initial promotion supports a reviewed tracing-only baseline")
        if baseline.deadline_at is not None or candidate.deadline_at is not None:
            raise ValueError("absolute process deadlines are not portable rollout configuration")
        if (
            type(thresholds) is not PromotionThresholds
            or type(canary) is not CanaryLimits
            or type(capability) is not CapabilityEvidence
        ):
            raise ValueError("typed thresholds, canary and capability evidence required")
        if canary.min_quality_score < thresholds.min_quality_score:
            raise ValueError("canary quality threshold cannot weaken reviewed quality")
        frozen = AblationProtocol.from_dict(
            protocol.to_dict() if isinstance(protocol, AblationProtocol) else protocol
        )
        specification = frozen.to_dict()["specification"]
        if (
            len(specification["tasks"]) > 1000
            or len(specification["schedule"]) > 5000
            or specification["bootstrap"]["samples"] > 10000
        ):
            raise ValueError("promotion study exceeds bounded offline evaluation limits")
        if not isinstance(observations, list) or len(observations) > 100000:
            raise ValueError("promotion observations must be a bounded list")
        timestamp(valid_until)
        if not isinstance(calibration_cohort, str) or not calibration_cohort:
            raise ValueError("explicit calibration cohort reference required")
        data = {
            "schema_version": "1.0",
            "proposal_id": proposal_id,
            "family": "budget",
            "baseline": asdict(baseline),
            "candidate": asdict(candidate),
            "baseline_mode": "disabled",
            "policy_config_hash": budget_policy(candidate).config_hash,
            "protocol": frozen.to_dict(),
            "observations": observations,
            "harness_evidence": {} if harness_evidence is None else harness_evidence,
            "calibration": calibration,
            "calibration_cohort": calibration_cohort,
            "capability": asdict(capability),
            "thresholds": asdict(thresholds),
            "canary": asdict(canary),
            "valid_until": valid_until,
        }
        encoded = canonical(data)
        if len(encoded.encode("utf-8")) > 20_000_000:
            raise ValueError("promotion evidence exceeds twenty million bytes")
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "proposal_hash", fingerprint(data))

    def to_dict(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class OperatorApproval:
    proposal_hash: str
    reviewer: str
    review_ref: str
    approved: bool
    scopes: tuple[str, ...]
    approved_at: str
    valid_until: str

    def __post_init__(self):
        digest(self.proposal_hash, "proposal_hash")
        identifier(self.reviewer, "reviewer")
        identifier(self.review_ref, "review_ref")
        if type(self.approved) is not bool:
            raise ValueError("operator decision must be explicit")
        if not isinstance(self.scopes, (tuple, list)) or not 1 <= len(self.scopes) <= 64:
            raise ValueError("approval needs bounded explicit rollout scopes")
        for scope in self.scopes:
            identifier(scope, "scope")
        object.__setattr__(self, "scopes", tuple(sorted(set(self.scopes))))
        if timestamp(self.approved_at) >= timestamp(self.valid_until):
            raise ValueError("operator approval must have a finite validity window")
