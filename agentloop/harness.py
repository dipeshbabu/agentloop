"""Opt-in lifecycle controls for explicitly wrapped Python callables.

Policies are synchronous trusted application code. The harness is neither a
sandbox nor an interceptor for calls outside its wrappers. See docs/HARNESS.md.
"""

from __future__ import annotations

import inspect
import json
import math
import re
from asyncio import CancelledError
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from hashlib import sha256
from threading import RLock
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, TypeVar
from uuid import uuid4
from weakref import ReferenceType, ref

from agentloop.budget_types import BudgetSnapshot, DispatchOptions, ResourceUsage
from agentloop.events import new_run_id, utc_now_iso
from agentloop.harness_evidence import (
    METADATA_KEY,
    HarnessEvidenceError,
    append_records,
    canonical_json,
    decision_id,
    empty_evidence,
    policy_snapshot,
    records_for_hook,
    valid_decision_id,
)
from agentloop.harness_usage import ObservedGenerator, UsageTracker
from agentloop.tracer import AgentTrace, current_event_id, current_trace

CONTRACT_VERSION = "1.0"
BOUNDARIES = frozenset({"model", "tool", "iteration", "completion"})
PHASES = frozenset({"before", "after"})
ACTIONS = frozenset({"continue", "deny", "stop", "escalate"})
EXECUTION_KINDS = frozenset({"sync", "async", "generator", "async_generator"})
_PRECEDENCE = {"continue": 0, "deny": 1, "stop": 2, "escalate": 3}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_IN_POLICY: ContextVar[bool] = ContextVar("agentloop_in_harness_policy", default=False)
F = TypeVar("F", bound=Callable[..., Any])


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier, not free-form content")


def _reference(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("configuration keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("configuration must contain only finite JSON values")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _callable_kind(function: Callable[..., Any]) -> str:
    for kind, check in (
        ("async_generator", inspect.isasyncgenfunction),
        ("generator", inspect.isgeneratorfunction),
        ("async", inspect.iscoroutinefunction),
    ):
        if check(function) or check(getattr(function, "__call__", None)):
            return kind
    return "sync"


@dataclass(frozen=True, order=True)
class Hook:
    """One caller-defined dispatch boundary and lifecycle phase."""

    boundary: str
    phase: str = "before"

    def __post_init__(self) -> None:
        if self.boundary not in BOUNDARIES or self.phase not in PHASES:
            raise ValueError("unsupported harness hook")


ALL_HOOKS = frozenset(Hook(boundary, phase) for boundary in BOUNDARIES for phase in PHASES)


@dataclass(frozen=True)
class Decision:
    """A bounded proposal with no arguments, outputs, or exception details."""

    action: str = "continue"
    reason_code: str = "allowed"
    evidence_refs: tuple[str, ...] = ()
    retry_of: str | None = None
    budget_snapshot: BudgetSnapshot | None = None
    feedback: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("unsupported harness action")
        _identifier(self.reason_code, "reason_code")
        if self.feedback is not None and (
            not isinstance(self.feedback, str) or not self.feedback or len(self.feedback) > 256
        ):
            raise ValueError("feedback must be nonempty text of at most 256 characters")
        if not isinstance(self.evidence_refs, (tuple, list, set, frozenset)):
            raise ValueError("evidence_refs must be a collection of identifiers")
        refs = tuple(self.evidence_refs)
        for value in refs:
            _reference(value, "evidence reference")
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(refs))))
        if self.retry_of is not None and not valid_decision_id(self.retry_of):
            raise ValueError("retry_of must reference a harness decision ID")
        if self.budget_snapshot is not None:
            if type(self.budget_snapshot) is not BudgetSnapshot:
                raise ValueError("budget_snapshot must be a BudgetSnapshot")
            BudgetSnapshot.from_dict(self.budget_snapshot.to_dict())


@dataclass(frozen=True)
class HookContext:
    """Safe call identity plus one policy's synchronized, run-local state."""

    run_id: str
    call_id: str
    branch_id: str
    hook: Hook
    mode: str
    status: str
    trace_id: str | None
    parent_span_id: str | None
    configuration: Mapping[str, Any]
    state: dict[str, Any] = field(repr=False, compare=False)
    dispatched: bool
    decision_id: str = ""
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)
    usage: ResourceUsage | None = None
    usage_error: str | None = None


@dataclass(frozen=True)
class Policy:
    """Versioned callback and immutable declarations checked before enforcement."""

    policy_id: str
    version: str
    evaluate: Callable[[HookContext], Decision] = field(repr=False, compare=False)
    hooks: frozenset[Hook] = field(default_factory=lambda: frozenset({Hook("model")}))
    actions: frozenset[str] = field(default_factory=lambda: frozenset({"continue"}))
    priority: int = 0
    configuration: Mapping[str, Any] = field(default_factory=dict, repr=False)
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _identifier(self.version, "policy version")
        if not callable(self.evaluate) or _callable_kind(self.evaluate) != "sync":
            raise ValueError("policy evaluate must be a synchronous callable")
        if type(self.priority) is not int:
            raise ValueError("policy priority must be an integer")
        hooks, actions = frozenset(self.hooks), frozenset(self.actions)
        if not hooks or any(not isinstance(hook, Hook) for hook in hooks):
            raise ValueError("policy must declare supported Hook objects")
        if not actions or not actions <= ACTIONS:
            raise ValueError("policy must declare supported actions")
        if "deny" in actions and not any(hook.phase == "before" for hook in hooks):
            raise ValueError("deny requires a before hook")
        if not isinstance(self.configuration, Mapping):
            raise ValueError("policy configuration must be a JSON object")
        configuration = _json_value(self.configuration)
        object.__setattr__(self, "hooks", hooks)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "configuration", _freeze(configuration))
        object.__setattr__(
            self,
            "config_hash",
            _hash(
                {
                    "contract_version": CONTRACT_VERSION,
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "priority": self.priority,
                    "hooks": [(hook.boundary, hook.phase) for hook in sorted(hooks)],
                    "actions": sorted(actions),
                    "configuration": configuration,
                }
            ),
        )


@dataclass(frozen=True)
class HarnessConfig:
    """A deterministic, immutable policy configuration; controls default to off."""

    mode: str = "disabled"
    policies: tuple[Policy, ...] = ()
    schema_version: str = CONTRACT_VERSION
    capture_policy_configuration: bool = False
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.mode not in {"disabled", "shadow", "enforce"}:
            raise ValueError("mode must be disabled, shadow, or enforce")
        if self.schema_version != CONTRACT_VERSION:
            raise ValueError("unsupported harness configuration version")
        if type(self.capture_policy_configuration) is not bool:
            raise ValueError("capture_policy_configuration must be a boolean")
        policies = tuple(self.policies)
        if any(not isinstance(policy, Policy) for policy in policies):
            raise ValueError("policies must be Policy objects")
        if len({policy.policy_id for policy in policies}) != len(policies):
            raise ValueError("policy IDs must be unique")
        policies = tuple(sorted(policies, key=lambda policy: (policy.priority, policy.policy_id)))
        object.__setattr__(self, "policies", policies)
        object.__setattr__(
            self,
            "config_hash",
            _hash(
                {
                    "schema_version": self.schema_version,
                    "mode": self.mode,
                    "policies": [policy.config_hash for policy in policies],
                    **(
                        {"capture_policy_configuration": True}
                        if self.capture_policy_configuration
                        else {}
                    ),
                }
            ),
        )


@dataclass(frozen=True)
class AdapterCapabilities:
    """Explicit supported hooks, actions, and callable lifecycles."""

    name: str
    hooks: frozenset[Hook]
    actions: frozenset[str]
    execution_kinds: frozenset[str]

    def __post_init__(self) -> None:
        _identifier(self.name, "adapter name")
        for key in ("hooks", "actions", "execution_kinds"):
            object.__setattr__(self, key, frozenset(getattr(self, key)))
        if not self.hooks <= ALL_HOOKS or not self.actions <= ACTIONS:
            raise ValueError("unsupported adapter capability")
        if not self.execution_kinds <= EXECUTION_KINDS:
            raise ValueError("unsupported callable lifecycle")


PYTHON_CAPABILITIES = AdapterCapabilities("python", ALL_HOOKS, ACTIONS, EXECUTION_KINDS)


@dataclass(frozen=True)
class PolicyResult:
    """One policy proposal, including safe policy-error evidence."""

    policy_id: str
    policy_version: str
    config_hash: str
    decision: Decision
    failed: bool = False
    started_at: str | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class HookResult:
    """In-memory proposals and resolution, not a measured intervention outcome."""

    run_id: str
    call_id: str
    branch_id: str
    hook: Hook
    mode: str
    status: str
    trace_id: str | None
    parent_span_id: str | None
    proposals: tuple[PolicyResult, ...]
    action: str
    applied: bool
    dispatched: bool
    started_at: str | None = None
    duration_ms: float | None = None
    sequence: int | None = None


class HarnessControlError(RuntimeError):
    """Base for explicit control flow that the host application must handle."""

    def __init__(self, result: HookResult) -> None:
        self.result = result
        super().__init__(f"Harness {result.action} at {result.hook.boundary}/{result.hook.phase}")


class HarnessDeniedError(HarnessControlError):
    """The protected callable was not admitted."""


class HarnessStoppedError(HarnessControlError):
    """The run no longer admits new work; existing work is not interrupted."""


class HarnessEscalationError(HarnessControlError):
    """The run requires host intervention before further work can be admitted."""


@dataclass(frozen=True)
class Harness:
    """Factory for independent runs sharing immutable settings, never run state."""

    config: HarnessConfig = field(default_factory=HarnessConfig)
    capabilities: AdapterCapabilities = PYTHON_CAPABILITIES
    _policies_by_hook: Mapping[Hook, tuple[Policy, ...]] = field(
        init=False, repr=False, compare=False
    )
    _snapshot_json: Mapping[str, str] = field(init=False, repr=False, compare=False)
    _system_snapshot_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, HarnessConfig) or not isinstance(
            self.capabilities, AdapterCapabilities
        ):
            raise ValueError("harness requires configuration and adapter capabilities")
        if (
            self.config.mode == "enforce"
            and not {"continue", "stop", "escalate"} <= self.capabilities.actions
        ):
            raise ValueError("adapter actions must support fail-closed stop and escalation")
        if self.config.mode != "disabled":
            for policy in self.config.policies:
                if not policy.hooks <= self.capabilities.hooks:
                    raise ValueError("adapter does not support every declared policy hook")
                if not policy.actions <= self.capabilities.actions:
                    raise ValueError("adapter does not support every declared policy action")
        object.__setattr__(
            self,
            "_policies_by_hook",
            MappingProxyType(
                {
                    hook: tuple(policy for policy in self.config.policies if hook in policy.hooks)
                    for hook in ALL_HOOKS
                }
            ),
        )
        snapshots = {
            policy.config_hash: canonical_json(
                policy_snapshot(
                    policy, include_configuration=self.config.capture_policy_configuration
                )
            )
            for policy in self.config.policies
        }
        system = {
            "policy_id": "agentloop.harness",
            "version": CONTRACT_VERSION,
            "config_hash": _hash(
                {
                    "harness_config_hash": self.config.config_hash,
                    "adapter": self.capabilities.name,
                    "hooks": [
                        (hook.boundary, hook.phase) for hook in sorted(self.capabilities.hooks)
                    ],
                    "actions": sorted(self.capabilities.actions),
                    "execution_kinds": sorted(self.capabilities.execution_kinds),
                }
            ),
            "priority": 0,
            "hooks": [
                {"boundary": hook.boundary, "phase": hook.phase} for hook in sorted(ALL_HOOKS)
            ],
            "actions": sorted(ACTIONS),
            "configuration_capture": "redacted",
            "configuration": None,
        }
        serialized = canonical_json(system)
        snapshots[system["config_hash"]] = serialized
        object.__setattr__(self, "_snapshot_json", MappingProxyType(snapshots))
        object.__setattr__(self, "_system_snapshot_json", serialized)

    def start_run(self, run_id: str | None = None) -> HarnessRun:
        """Create fresh state, using the active trace's identity when available."""
        trace = current_trace()
        identity = run_id if run_id is not None else trace.run_id if trace else new_run_id()
        _reference(identity, "run_id")
        return HarnessRun(self, identity)


@dataclass(frozen=True)
class _Invocation:
    call_id: str
    boundary: str
    branch_id: str
    trace_id: str | None
    parent_span_id: str | None
    trace: AgentTrace | None = field(repr=False, compare=False)
    dispatch: DispatchOptions
    usage: UsageTracker = field(repr=False, compare=False)


@dataclass(frozen=True)
class _WrapperMarker:
    run: HarnessRun
    boundary: str
    branch_id: str
    function: ReferenceType
    dispatch: DispatchOptions
    usage_reader: Callable | None


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, GeneratorExit):
        return "closed"
    if isinstance(exc, CancelledError):
        return "cancelled"
    return "error"


def _invoke_usage_reader(reader: Callable, value: Any):
    token = _IN_POLICY.set(True)
    try:
        return reader(value)
    finally:
        _IN_POLICY.reset(token)


class HarnessRun:
    """Explicit run ownership with serialized hooks and concurrent protected work."""

    def __init__(self, harness: Harness, run_id: str) -> None:
        self._harness = harness
        self._run_id = run_id
        self._lock = RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._results: list[HookResult] = []
        self._stopped = False
        self._evidence = empty_evidence()
        self._snapshots = {key: json.loads(value) for key, value in harness._snapshot_json.items()}
        self._system_snapshot = json.loads(harness._system_snapshot_json)

    @property
    def harness(self) -> Harness:
        """The immutable factory/configuration captured when this run was created."""
        return self._harness

    @property
    def run_id(self) -> str:
        """The host's stable identity for this run."""
        return self._run_id

    @property
    def results(self) -> tuple[HookResult, ...]:
        """Return an immutable snapshot of recorded hooks."""
        with self._lock:
            return tuple(self._results)

    @property
    def stopped(self) -> bool:
        """Whether enforcement has stopped new admissions for this run."""
        with self._lock:
            return self._stopped

    def export_evidence(self) -> dict[str, Any]:
        """Return a detached artifact, including any safe trace-capture diagnostics."""
        with self._lock:
            return json.loads(canonical_json(self._evidence))

    def _record_evidence(self, invocation: _Invocation, result: HookResult) -> None:
        records = records_for_hook(result, self.harness.config.config_hash, self._system_snapshot)
        append_records(self._evidence, records, self._snapshots)
        if invocation.trace is None:
            return
        try:
            envelope = invocation.trace.metadata.setdefault(METADATA_KEY, empty_evidence())
            append_records(envelope, records, self._snapshots)
        except (HarnessEvidenceError, AttributeError, TypeError, KeyError):
            self._evidence["capture_errors"].append(
                {
                    "call_id": invocation.call_id,
                    "phase": result.hook.phase,
                    "category": "trace_evidence_error",
                }
            )
            if self.harness.config.mode == "enforce":
                self._stopped = True
                raise HarnessEvidenceError("trace evidence capture failed") from None

    def _hook(
        self, invocation: _Invocation, phase: str, status: str, *, dispatched: bool = False
    ) -> HookResult:
        hook = Hook(invocation.boundary, phase)
        config = self.harness.config
        started_at = utc_now_iso()
        started_ns = perf_counter_ns()
        with self._lock:
            proposals = []
            fatal: BaseException | None = None
            action = "stop" if self._stopped and phase == "before" else "continue"
            if action == "continue":
                for policy in self.harness._policies_by_hook[hook]:
                    context = HookContext(
                        self.run_id,
                        invocation.call_id,
                        invocation.branch_id,
                        hook,
                        config.mode,
                        status,
                        invocation.trace_id,
                        invocation.parent_span_id,
                        policy.configuration,
                        self._states.setdefault(policy.policy_id, {}),
                        dispatched,
                        decision_id(
                            self.run_id,
                            invocation.call_id,
                            hook.boundary,
                            hook.phase,
                            policy.policy_id,
                        ),
                        invocation.dispatch,
                        invocation.usage.latest,
                        invocation.usage.error,
                    )
                    token = _IN_POLICY.set(True)
                    failed = False
                    policy_started_at = utc_now_iso()
                    policy_started_ns = perf_counter_ns()
                    try:
                        decision = policy.evaluate(context)
                        if (
                            isinstance(decision, Decision)
                            and decision.budget_snapshot is not None
                            and config.mode == "shadow"
                        ):
                            budget = decision.budget_snapshot.to_dict()
                            if any(
                                budget[key] == "best_effort"
                                for key in ("spend_enforcement", "token_enforcement")
                            ):
                                raise ValueError("shadow budget evidence cannot claim enforcement")
                        if (
                            not isinstance(decision, Decision)
                            or decision.action not in policy.actions
                            or (phase == "after" and decision.action == "deny")
                            or decision.retry_of == context.decision_id
                        ):
                            if inspect.iscoroutine(decision):
                                decision.close()
                            raise ValueError("policy returned an undeclared decision")
                    except BaseException as exc:
                        decision = Decision("escalate", "policy_error")
                        failed = True
                        if not isinstance(exc, Exception):
                            fatal = exc
                    finally:
                        _IN_POLICY.reset(token)
                    proposals.append(
                        PolicyResult(
                            policy.policy_id,
                            policy.version,
                            policy.config_hash,
                            decision,
                            failed,
                            policy_started_at,
                            max(0, perf_counter_ns() - policy_started_ns) / 1_000_000,
                        )
                    )
                    if _PRECEDENCE[decision.action] > _PRECEDENCE[action]:
                        action = decision.action
                    if fatal is not None:
                        break
            if config.mode == "enforce" and action in {"stop", "escalate"}:
                self._stopped = True
            result = HookResult(
                self.run_id,
                invocation.call_id,
                invocation.branch_id,
                hook,
                config.mode,
                status,
                invocation.trace_id,
                invocation.parent_span_id,
                tuple(proposals),
                action,
                config.mode == "enforce" and action != "continue",
                dispatched,
                started_at,
                max(0, perf_counter_ns() - started_ns) / 1_000_000,
                len(self._results),
            )
            self._results.append(result)
            try:
                self._record_evidence(invocation, result)
            except HarnessEvidenceError:
                if fatal is None:
                    raise
            if fatal is not None:
                raise fatal
            return result

    @staticmethod
    def _apply(result: HookResult) -> None:
        if not result.applied:
            return
        error = {
            "deny": HarnessDeniedError,
            "stop": HarnessStoppedError,
            "escalate": HarnessEscalationError,
        }
        raise error[result.action](result)

    def _begin(
        self,
        boundary: str,
        branch_id: str,
        dispatch: DispatchOptions,
        usage_reader: Callable | None,
    ) -> _Invocation:
        if _IN_POLICY.get():
            raise RuntimeError("protected dispatch from a policy is unsupported")
        trace = current_trace()
        invocation = _Invocation(
            f"call_{uuid4().hex}",
            boundary,
            branch_id,
            trace.run_id if trace else None,
            current_event_id() if trace else None,
            trace,
            dispatch,
            UsageTracker(usage_reader, _invoke_usage_reader),
        )
        try:
            self._apply(self._hook(invocation, "before", "pending"))
        except BaseException as exc:
            status = "denied" if isinstance(exc, HarnessControlError) else _failure_status(exc)
            self._finish(invocation, status, exc, dispatched=False)
            raise
        return invocation

    def _finish(
        self,
        invocation: _Invocation,
        status: str,
        original: BaseException | None = None,
        *,
        dispatched: bool = True,
    ) -> None:
        try:
            self._apply(self._hook(invocation, "after", status, dispatched=dispatched))
        except BaseException:
            if original is None:
                raise
            # Preserve an original failure/cancellation. The hook result has
            # already recorded the failure and stopped future enforced work.

    def wrap(
        self,
        function: F,
        *,
        boundary: str,
        branch_id: str = "main",
        dispatch: DispatchOptions | None = None,
        usage_reader: Callable[[Any], ResourceUsage | None] | None = None,
    ) -> F:
        """Wrap an explicit dispatch, preserving each Python callable lifecycle."""
        if not callable(function):
            raise ValueError("protected work must be callable")
        Hook(boundary)
        _reference(branch_id, "branch_id")
        options = DispatchOptions() if dispatch is None else dispatch
        if type(options) is not DispatchOptions:
            raise ValueError("dispatch must be DispatchOptions")
        if usage_reader is not None and (
            not callable(usage_reader) or _callable_kind(usage_reader) != "sync"
        ):
            raise ValueError("usage_reader must be a synchronous callable")
        if self.harness.config.mode == "disabled":
            return function
        marker = getattr(function, "__agentloop_harness__", None)
        if (
            isinstance(marker, _WrapperMarker)
            and marker.run is self
            and marker.boundary == boundary
            and marker.branch_id == branch_id
            and marker.function() is function
        ):
            if marker.dispatch == options and marker.usage_reader is usage_reader:
                return function
            raise ValueError("wrap the original callable to change dispatch or usage options")
        kind = _callable_kind(function)
        if kind not in self.harness.capabilities.execution_kinds:
            raise ValueError("adapter does not support this callable lifecycle")
        if not {Hook(boundary), Hook(boundary, "after")} <= self.harness.capabilities.hooks:
            raise ValueError("adapter does not support this dispatch boundary's lifecycle")

        if kind == "async_generator":

            @wraps(function)
            async def wrapped(*args, **kwargs):
                invocation = self._begin(boundary, branch_id, options, usage_reader)
                try:
                    generator = function(*args, **kwargs)
                    item = await generator.__anext__()
                    while True:
                        invocation.usage.observe(item)
                        try:
                            sent = yield item
                        except GeneratorExit:
                            await generator.aclose()
                            raise
                        except BaseException as exc:
                            item = await generator.athrow(exc)
                        else:
                            item = await generator.asend(sent)
                except StopAsyncIteration:
                    self._finish(invocation, "ok")
                except BaseException as exc:
                    self._finish(invocation, _failure_status(exc), exc)
                    raise
        elif kind == "generator":

            @wraps(function)
            def wrapped(*args, **kwargs):
                invocation = self._begin(boundary, branch_id, options, usage_reader)
                try:
                    source = function(*args, **kwargs)
                    result = yield from (
                        source
                        if usage_reader is None
                        else ObservedGenerator(source, invocation.usage)
                    )
                except BaseException as exc:
                    self._finish(invocation, _failure_status(exc), exc)
                    raise
                self._finish(invocation, "ok")
                return result
        elif kind == "async":

            @wraps(function)
            async def wrapped(*args, **kwargs):
                invocation = self._begin(boundary, branch_id, options, usage_reader)
                try:
                    result = await function(*args, **kwargs)
                    invocation.usage.observe(result)
                except BaseException as exc:
                    self._finish(invocation, _failure_status(exc), exc)
                    raise
                self._finish(invocation, "ok")
                return result
        else:

            @wraps(function)
            def wrapped(*args, **kwargs):
                invocation = self._begin(boundary, branch_id, options, usage_reader)
                try:
                    result = function(*args, **kwargs)
                    invocation.usage.observe(result)
                except BaseException as exc:
                    self._finish(invocation, _failure_status(exc), exc)
                    raise
                self._finish(invocation, "ok")
                return result

        wrapped.__agentloop_harness__ = _WrapperMarker(
            self, boundary, branch_id, ref(wrapped), options, usage_reader
        )
        return wrapped  # type: ignore[return-value]
