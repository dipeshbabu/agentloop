"""AgentLoop-owned contracts for optional offline semantic judgments."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Protocol

JUDGMENT_VERSION = "1.0"
JUDGMENT_KEY = "agentloop.judgments"


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("judgment data must contain finite JSON values") from None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value: Any, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{name} must be nonempty text of at most 4096 characters")


def finite(value: Any) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def is_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class JudgeIdentity:
    implementation: str
    version: str
    config_hash: str
    provider: str = "local"
    model_or_rule: str = "callback"
    revision: str = "1"

    def __post_init__(self):
        for key, value in asdict(self).items():
            text(value, key)
        if not is_hash(self.config_hash):
            raise ValueError("config_hash must be a SHA-256 digest")

    @classmethod
    def configured(cls, implementation: str, version: str, configuration: Any, **kwargs):
        """Hash caller-owned configuration without retaining or forwarding its contents."""
        return cls(implementation, version, fingerprint(configuration), **kwargs)


@dataclass(frozen=True)
class JudgmentSpec:
    question: str
    kind: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self):
        text(self.question, "question")
        if self.kind not in {"boolean", "probability", "choice", "score"}:
            raise ValueError("unsupported judgment kind")
        if not isinstance(self.choices, (list, tuple)):
            raise ValueError("choices must be a list or tuple")
        object.__setattr__(self, "choices", tuple(self.choices))
        if self.kind == "choice":
            for item in self.choices:
                text(item, "choice")
            if not self.choices or len(set(self.choices)) != len(self.choices):
                raise ValueError("choices must be nonempty and unique")
        elif self.choices:
            raise ValueError("choices apply only to choice judgments")
        if self.kind == "score":
            if not finite(self.minimum) or not finite(self.maximum) or self.minimum >= self.maximum:
                raise ValueError("score requires finite minimum < maximum")
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("explicit bounds apply only to score judgments")

    def accepts(self, value: Any) -> bool:
        if self.kind == "boolean":
            return type(value) is bool
        if self.kind == "choice":
            return isinstance(value, str) and value in self.choices
        if not finite(value):
            return False
        low, high = (0, 1) if self.kind == "probability" else (self.minimum, self.maximum)
        return low <= value <= high


@dataclass(frozen=True)
class JudgmentEvidence:
    span_id: str
    source_hash: str
    operation_kind: str
    status: str
    duration_ms: float
    summary: str | None = None

    def __post_init__(self):
        text(self.span_id, "span_id")
        text(self.operation_kind, "operation_kind")
        if not is_hash(self.source_hash):
            raise ValueError("source_hash must be a SHA-256 digest")
        if self.status not in {"ok", "error", "unknown"}:
            raise ValueError("unsupported observed status")
        if not finite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("duration_ms must be finite and nonnegative")
        text(self.summary, "summary", optional=True)


@dataclass(frozen=True)
class JudgmentRequest:
    run_id: str
    spec: JudgmentSpec
    evidence: tuple[JudgmentEvidence, ...]

    def __post_init__(self):
        text(self.run_id, "run_id")
        if type(self.spec) is not JudgmentSpec:
            raise ValueError("spec must be JudgmentSpec")
        if not isinstance(self.evidence, (tuple, list)):
            raise ValueError("evidence must be a list or tuple")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence or any(type(item) is not JudgmentEvidence for item in self.evidence):
            raise ValueError("at least one JudgmentEvidence is required")
        if len({item.span_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("evidence span IDs must be unique")

    def to_dict(self):
        return json.loads(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value):
        return cls(
            value["run_id"],
            JudgmentSpec(**value["spec"]),
            tuple(JudgmentEvidence(**item) for item in value["evidence"]),
        )


@dataclass(frozen=True)
class JudgeUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    token_basis: str = "unknown"
    cost_basis: str = "unknown"

    def __post_init__(self):
        for count in (self.input_tokens, self.output_tokens):
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("known token counts must be nonnegative integers")
        if self.cost_usd is not None and (not finite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("known cost must be finite and nonnegative")
        if self.token_basis not in {"unknown", "reported", "estimated", "not_dispatched"}:
            raise ValueError("unsupported token basis")
        if self.cost_basis not in {"unknown", "reported", "calculated", "not_dispatched"}:
            raise ValueError("unsupported cost basis")
        if (self.input_tokens is not None or self.output_tokens is not None) == (
            # A model-usage provenance label, not an authentication credential.
            self.token_basis == "unknown"  # nosec B105
        ):
            raise ValueError("token counts and basis must agree")
        if (self.cost_usd is not None) == (self.cost_basis == "unknown"):
            raise ValueError("cost and basis must agree")
        # This public label records that no model invocation occurred.
        if self.token_basis == "not_dispatched" and (self.input_tokens, self.output_tokens) != (  # nosec B105
            0,
            0,
        ):
            raise ValueError("undispatched usage must be zero")
        if self.cost_basis == "not_dispatched" and self.cost_usd != 0:
            raise ValueError("undispatched cost must be zero")


@dataclass(frozen=True)
class JudgmentUncertainty:
    confidence: float | None = None
    calibration_status: str = "uncalibrated"
    calibration_ref: str | None = None

    def __post_init__(self):
        if self.confidence is not None and (
            not finite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be in [0, 1]")
        if self.calibration_status not in {"uncalibrated", "measured"}:
            raise ValueError("unsupported calibration status")
        text(self.calibration_ref, "calibration_ref", optional=True)
        if self.calibration_status == "measured" and self.calibration_ref is None:
            raise ValueError("measured calibration requires a caller-supplied evidence reference")


@dataclass(frozen=True)
class JudgmentAnswer:
    value: Any = None
    status: str = "known"
    reason: str | None = None
    usage: JudgeUsage = JudgeUsage()
    uncertainty: JudgmentUncertainty = JudgmentUncertainty()

    def __post_init__(self):
        if self.status not in {"known", "unknown"}:
            raise ValueError("answer status must be known or unknown")
        if self.status == "unknown":
            if self.value is not None or self.reason not in {"abstained", "insufficient_evidence"}:
                raise ValueError("unknown answers require a reason and no value")
        elif self.reason is not None:
            raise ValueError("known answers cannot carry a failure reason")
        if type(self.usage) is not JudgeUsage or type(self.uncertainty) is not JudgmentUncertainty:
            raise ValueError("answer requires typed usage and uncertainty")
        if self.value is not None and type(self.value) not in {str, bool, int, float}:
            raise ValueError("answer must be a scalar")
        canonical(self.value)


class JudgmentBackend(Protocol):
    """Adapters own cooperative time limits and must return a typed answer."""

    @property
    def identity(self) -> JudgeIdentity: ...

    def judge(self, request: JudgmentRequest, *, timeout_s: float | None) -> JudgmentAnswer: ...
