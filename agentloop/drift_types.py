"""Explicit window, quality and threshold declarations for local drift reports."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime


def label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty reference of at most 256 characters")


def timestamp(value):
    label(value, "timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("window timestamps require a timezone")
    return result


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    version: str
    started_at: str
    ended_at: str
    workload_version: str
    config_version: str
    model_version: str

    def __post_init__(self):
        for key, value in asdict(self).items():
            label(value, key)
        if timestamp(self.started_at) >= timestamp(self.ended_at):
            raise ValueError("window end must follow its start")


@dataclass(frozen=True)
class QualityMetric:
    name: str
    kind: str
    version: str
    source_ref: str

    def __post_init__(self):
        for key, value in asdict(self).items():
            label(value, key)
        if self.kind not in {"quality", "proxy"}:
            raise ValueError("quality metric kind must be quality or proxy")


@dataclass(frozen=True)
class DriftRule:
    metric: str
    direction: str = "increase"
    absolute: float | None = None
    relative_pct: float | None = None
    noise_margin: float = 0.0
    minimum_observations: int = 20
    minimum_coverage: float = 1.0

    def __post_init__(self):
        label(self.metric, "metric")
        allowed = {
            "latency_mean_ms",
            "latency_p95_ms",
            "input_tokens_mean",
            "output_tokens_mean",
            "known_cost_mean_usd",
            "failure_rate",
            "timeout_rate",
            "abstention_rate",
            "finding_incidence",
            "instrumentation_complete_rate",
            "model_mix",
            "stage_mix",
            "outcome_mix",
            "decision_outcome_mix",
        }
        if self.metric not in allowed and not self.metric.startswith("quality:"):
            raise ValueError("unsupported drift metric")
        if self.metric.startswith("quality:"):
            label(self.metric.partition(":")[2], "quality name")
        if self.direction not in {"increase", "decrease"}:
            raise ValueError("direction must be increase or decrease")
        if self.absolute is None and self.relative_pct is None:
            raise ValueError("at least one absolute or relative threshold is required")
        for key in ("absolute", "relative_pct", "noise_margin", "minimum_coverage"):
            value = getattr(self, key)
            if value is None and key in {"absolute", "relative_pct"}:
                continue
            try:
                valid = type(value) in {int, float} and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{key} must be finite and nonnegative")
        if not 0 < self.minimum_coverage <= 1:
            raise ValueError("minimum_coverage must be in (0,1]")
        if (
            type(self.minimum_observations) is not int
            or not 2 <= self.minimum_observations <= 1_000_000
        ):
            raise ValueError("minimum_observations must be 2..1000000")
        if self.metric.endswith("_mix") and (
            self.direction != "increase" or self.absolute is None or self.relative_pct is not None
        ):
            raise ValueError("mix drift requires an absolute increase threshold")
