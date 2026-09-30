"""Versioned, bounded configuration for offline and finalization retention."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

RETENTION_KEY = "agentloop.retention"
RETENTION_VERSION = "1.0"


@dataclass(frozen=True)
class RetentionPolicy:
    """Labels and references must be opaque, permission-cleared identifiers."""

    policy_id: str
    version: str
    mode: Literal["full", "compact", "metrics_only"] = "compact"
    sampling: Literal["deterministic", "probabilistic"] = "deterministic"
    sample_rate: float = 1.0
    seed: str = "agentloop"
    max_events: int = 100
    payloads: Literal["omit", "redact", "capture"] = "omit"
    redactor_ref: str | None = None
    retain_failures: bool = True
    retain_unknowns: bool = True
    retain_disagreements: bool = True
    retain_anomalies: bool = True
    protected_cohorts: tuple[str, ...] = ()
    high_cost_usd: float | None = None
    high_latency_ms: float | None = None
    representatives_per_bucket: int = 0
    max_buckets: int = 256

    def __post_init__(self) -> None:
        for key in ("policy_id", "version", "seed"):
            _label(getattr(self, key), key)
        for key, choices in (
            ("mode", {"full", "compact", "metrics_only"}),
            ("sampling", {"deterministic", "probabilistic"}),
            ("payloads", {"omit", "redact", "capture"}),
        ):
            if getattr(self, key) not in choices:
                raise ValueError(f"invalid {key}")
        for key in ("sample_rate", "high_cost_usd", "high_latency_ms"):
            value = getattr(self, key)
            if value is None and key != "sample_rate":
                continue
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
        if self.sample_rate > 1:
            raise ValueError("sample_rate must be at most 1")
        for key, minimum, maximum in (
            ("max_events", 0, 100_000),
            ("representatives_per_bucket", 0, 100),
            ("max_buckets", 1, 10_000),
        ):
            value = getattr(self, key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"invalid {key}")
        for key in (
            "retain_failures",
            "retain_unknowns",
            "retain_disagreements",
            "retain_anomalies",
        ):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be boolean")
        if not isinstance(self.protected_cohorts, tuple) or len(self.protected_cohorts) > 256:
            raise ValueError("protected_cohorts must be a tuple of at most 256 labels")
        for label in self.protected_cohorts:
            _label(label, "cohort")
        if self.payloads == "redact":
            _label(self.redactor_ref, "redactor_ref")
        elif self.redactor_ref is not None:
            raise ValueError("redactor_ref requires payloads='redact'")

    def to_dict(self) -> dict:
        return {
            "schema_version": RETENTION_VERSION,
            **asdict(self),
            "protected_cohorts": list(self.protected_cohorts),
        }


@dataclass(frozen=True)
class RetentionContext:
    """Host annotations; None is unknown, not a negative observation."""

    sample_key: str | None = None
    outcome: Literal["success", "failure", "timeout", "unknown"] = "unknown"
    cohorts: tuple[str, ...] = ()
    disagreement: bool | None = None
    anomaly: bool | None = None

    def __post_init__(self) -> None:
        if self.sample_key is not None:
            _label(self.sample_key, "sample_key")
        if self.outcome not in {"success", "failure", "timeout", "unknown"}:
            raise ValueError("invalid outcome")
        if not isinstance(self.cohorts, tuple) or len(self.cohorts) > 256:
            raise ValueError("cohorts must be a tuple of at most 256 labels")
        for value in self.cohorts:
            _label(value, "cohort")
        for value in (self.disagreement, self.anomaly):
            if value is not None and type(value) is not bool:
                raise ValueError("disagreement/anomaly must be boolean or None")


def _label(value, key):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{key} must be a nonempty bounded string")
