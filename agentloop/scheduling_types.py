"""Explicit tool dependencies, resource contracts and ordered outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import Event
from time import monotonic
from types import MappingProxyType
from typing import Any, Callable

from agentloop.budget_types import DispatchOptions, ResourceUsage
from agentloop.context_types import fingerprint, identifier, synchronous

SCHEDULE_KEY = "agentloop.tool_schedule"
SCHEDULE_VERSION = "1.0"


class SchedulingError(ValueError):
    def __init__(self, reason):
        allowed = {
            "invalid_plan",
            "duplicate_call",
            "missing_dependency",
            "duplicate_dependency",
            "cycle",
            "unknown_safety",
            "unknown_order",
            "already_started",
            "unsupported_lifecycle",
            "evidence_limit",
            "invalid_trace_evidence",
        }
        self.reason_code = (
            reason if isinstance(reason, str) and reason in allowed else "invalid_plan"
        )
        super().__init__(f"Tool scheduling failed: {self.reason_code}")


@dataclass(frozen=True)
class ScheduleConfig:
    policy_id: str
    version: str
    branch_id: str
    max_concurrency: int = 4
    on_unknown: str = "sequential"
    on_error: str = "stop"

    def __post_init__(self):
        for name in ("policy_id", "version", "branch_id"):
            identifier(getattr(self, name), name)
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 32:
            raise ValueError("max_concurrency must be 1..32")
        if self.on_unknown not in {"sequential", "error"} or self.on_error not in {
            "stop",
            "continue_independent",
        }:
            raise ValueError("unsupported scheduling policy")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    invoke: Callable[[ToolContext], Any] = field(repr=False, compare=False)
    depends_on: tuple[str, ...] | None = None
    reads: tuple[str, ...] | None = None
    writes: tuple[str, ...] | None = None
    concurrent: bool | None = None
    effect: str = "unknown"
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)
    usage_reader: Callable[[Any], ResourceUsage | None] | None = field(
        default=None, repr=False, compare=False
    )
    error_usage_reader: Callable[[BaseException], ResourceUsage | None] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self):
        identifier(self.call_id, "call_id")
        if not synchronous(self.invoke):
            raise ValueError("tool callback must be synchronous")
        for name, maximum in (("depends_on", 256), ("reads", 64), ("writes", 64)):
            values = getattr(self, name)
            if values is None:
                continue
            if not isinstance(values, (list, tuple)) or len(values) > maximum:
                raise ValueError("tool declarations must be bounded sequences")
            for value in values:
                identifier(value, name)
            if len(values) != len(set(values)):
                raise SchedulingError(
                    "duplicate_dependency" if name == "depends_on" else "invalid_plan"
                )
            object.__setattr__(self, name, tuple(values))
        if self.concurrent is not None and type(self.concurrent) is not bool:
            raise ValueError("concurrent capability must be boolean or unknown")
        if self.effect not in {"read_only", "mutating", "unknown"}:
            raise ValueError("unsupported effect declaration")
        if self.effect == "read_only" and self.writes:
            raise ValueError("read-only call cannot declare writes")
        if self.effect == "mutating" and self.writes == ():
            raise ValueError("mutating call requires written resource declarations")
        if type(self.dispatch) is not DispatchOptions:
            raise ValueError("dispatch must be DispatchOptions")
        for reader in (self.usage_reader, self.error_usage_reader):
            if reader is not None and not synchronous(reader):
                raise ValueError("usage readers must be synchronous")

    def declaration(self):
        dispatch = asdict(self.dispatch)
        if dispatch["step"] is not None:
            dispatch["step"]["step_id"] = "sha256:" + fingerprint(dispatch["step"]["step_id"])
        if dispatch["reservation"] is not None:
            dispatch["reservation"]["evidence_refs"] = [
                "sha256:" + fingerprint(value) for value in dispatch["reservation"]["evidence_refs"]
            ]
        return {
            "call_id": self.call_id,
            "depends_on": None if self.depends_on is None else list(self.depends_on),
            "reads": None if self.reads is None else list(self.reads),
            "writes": None if self.writes is None else list(self.writes),
            "concurrent": self.concurrent,
            "effect": self.effect,
            "dispatch": dispatch,
        }


@dataclass(frozen=True)
class ToolContext:
    call_id: str
    prerequisites: MappingProxyType = field(repr=False)
    _stop: Event = field(repr=False)
    _external: Event | None = field(repr=False)
    deadline: float | None = None

    @property
    def cancellation_requested(self):
        return (
            self._stop.is_set()
            or (self._external is not None and self._external.is_set())
            or (self.deadline is not None and monotonic() >= self.deadline)
        )

    @property
    def remaining_s(self):
        return None if self.deadline is None else max(0.0, self.deadline - monotonic())


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    status: str
    value: Any = field(default=None, repr=False, compare=False)
    error: BaseException | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ScheduledResult:
    schedule_id: str
    outcomes: tuple[ToolOutcome, ...]
    stop_reason: str | None = None

    @property
    def completed(self):
        return self.stop_reason is None and all(
            item.status == "completed" for item in self.outcomes
        )
