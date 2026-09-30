"""Owned completion candidates and explicitly versioned trusted check contracts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from threading import Event
from time import monotonic
from typing import Callable

from agentloop.budget_types import DispatchOptions
from agentloop.context_types import canonical, fingerprint, identifier, synchronous

COMPLETION_KEY = "agentloop.completion"
COMPLETION_VERSION = "1.0"


@dataclass(frozen=True, init=False)
class CompletionCandidate:
    _json: str = field(repr=False)
    present: bool
    already_streamed: bool

    def __init__(self, value=None, *, present=True, already_streamed=False):
        if type(present) is not bool or type(already_streamed) is not bool:
            raise ValueError("candidate flags must be booleans")
        encoded = canonical(value)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("completion candidate exceeds one million bytes")
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "present", present)
        object.__setattr__(self, "already_streamed", already_streamed)

    @property
    def value(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    feedback: str = field(default="", repr=False)

    def __post_init__(self):
        if type(self.passed) is not bool:
            raise ValueError("check pass must be boolean")
        if not isinstance(self.feedback, str) or len(self.feedback) > 1024:
            raise ValueError("check feedback must be at most 1024 characters")


@dataclass(frozen=True)
class VerificationFeedback:
    check_id: str
    status: str
    detail: str = field(default="", repr=False)
    trust_class: str = field(default="verification_data", init=False)


@dataclass(frozen=True)
class VerificationContext:
    deadline: float
    cancel_event: Event | None = field(default=None, repr=False)

    @property
    def remaining_s(self):
        return max(0.0, self.deadline - monotonic())

    @property
    def cancellation_requested(self):
        return self.cancel_event is not None and self.cancel_event.is_set()


@dataclass(frozen=True)
class CompletionCheck:
    check_id: str
    version: str
    invoke: Callable = field(repr=False, compare=False)
    configuration_hash: str | None = None
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)
    usage_reader: Callable | None = field(default=None, repr=False, compare=False)
    error_usage_reader: Callable | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        identifier(self.check_id, "check_id")
        identifier(self.version, "check version")
        _callbacks(self)
        if self.configuration_hash is not None and (
            not isinstance(self.configuration_hash, str)
            or len(self.configuration_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.configuration_hash)
        ):
            raise ValueError("configuration_hash must be a SHA-256 digest")

    def declaration(self):
        return {
            "id": self.check_id,
            "version": self.version,
            "configuration_hash": self.configuration_hash,
            "dispatch_hash": fingerprint(asdict(self.dispatch)),
        }


@dataclass(frozen=True)
class RepairBackend:
    invoke: Callable = field(repr=False, compare=False)
    boundary: str = "model"
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)
    usage_reader: Callable | None = field(default=None, repr=False, compare=False)
    error_usage_reader: Callable | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        _callbacks(self)
        if self.boundary not in {"model", "tool"}:
            raise ValueError("repair boundary must be model or tool")
        if self.dispatch.step is None or not self.dispatch.step.retry_safe:
            raise ValueError("repair requires explicit retry-safe StepInfo")


def _callbacks(value):
    if not synchronous(value.invoke):
        raise ValueError("completion callbacks must be synchronous")
    if type(value.dispatch) is not DispatchOptions:
        raise ValueError("dispatch must be DispatchOptions")
    for reader in (value.usage_reader, value.error_usage_reader):
        if reader is not None and not synchronous(reader):
            raise ValueError("usage readers must be synchronous")


@dataclass(frozen=True)
class CompletionConfig:
    policy_id: str
    version: str
    branch_id: str
    checks: tuple[CompletionCheck, ...]
    max_repairs: int = 0
    verification_timeout_s: float = 5.0
    total_timeout_s: float = 30.0
    on_failure: str = "stop"

    def __post_init__(self):
        for name in ("policy_id", "version", "branch_id"):
            identifier(getattr(self, name), name)
        if not isinstance(self.checks, (list, tuple)) or not 1 <= len(self.checks) <= 16:
            raise ValueError("configure between one and sixteen completion checks")
        if any(type(check) is not CompletionCheck for check in self.checks):
            raise ValueError("checks must be CompletionCheck objects")
        if len({check.check_id for check in self.checks}) != len(self.checks):
            raise ValueError("check IDs must be unique")
        object.__setattr__(self, "checks", tuple(self.checks))
        if type(self.max_repairs) is not int or not 0 <= self.max_repairs <= 8:
            raise ValueError("max_repairs must be between zero and eight")
        for timeout in (self.verification_timeout_s, self.total_timeout_s):
            if type(timeout) not in {int, float} or not 0 < timeout < float("inf"):
                raise ValueError("completion deadlines must be finite and positive")
        if self.on_failure not in {"stop", "escalate"}:
            raise ValueError("on_failure must be stop or escalate")

    def to_dict(self):
        return {
            "schema_version": COMPLETION_VERSION,
            "branch_id": self.branch_id,
            "checks": [check.declaration() for check in self.checks],
            "max_repairs": self.max_repairs,
            "verification_timeout_s": self.verification_timeout_s,
            "total_timeout_s": self.total_timeout_s,
            "on_failure": self.on_failure,
        }


@dataclass(frozen=True)
class CompletionResult:
    status: str
    candidate: CompletionCandidate = field(repr=False)
    verified: bool
    repairs: int


def quality_check(check_id, version, fixture):
    """Bind an owned deterministic quality fixture; never import executable scorers."""
    from agentloop.quality import score_output, validate_quality_fixtures

    encoded = canonical(fixture)
    owned = json.loads(encoded)
    if (
        not isinstance(owned, dict)
        or not isinstance(owned.get("scorer", {}), dict)
        or owned.get("scorer", {}).get("type") == "custom"
    ):
        raise ValueError("completion quality checks require deterministic fixtures")
    validate_quality_fixtures([owned])

    def invoke(candidate, context):
        item = json.loads(encoded)
        result = score_output(candidate.value, item, item.get("scorer", {"type": "exact_match"}))
        return CheckResult(
            result["passed"], "configured quality criterion failed" if not result["passed"] else ""
        )

    return CompletionCheck(check_id, version, invoke, fingerprint(owned))
