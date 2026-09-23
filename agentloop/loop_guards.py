"""Operational guardrails for declared retries and repeated caller state."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from agentloop.budget_types import RETRY_SOURCES, count
from agentloop.harness import BOUNDARIES, Decision, Hook, HookContext, Policy


@dataclass(frozen=True)
class LoopLimits:
    """Retry caps and optional bounded-history repetition/oscillation limits."""

    max_retries_per_step: int | None = 3
    max_identical_calls: int | None = None
    oscillation_period: int | None = None
    oscillation_repeats: int = 3

    def __post_init__(self) -> None:
        count(self.max_retries_per_step, "max_retries_per_step")
        count(self.max_identical_calls, "max_identical_calls")
        if self.max_identical_calls == 0:
            raise ValueError("max_identical_calls must allow at least one call")
        if self.max_identical_calls is not None and self.max_identical_calls > 256:
            raise ValueError("identical-call history is bounded to 256 calls")
        if self.oscillation_period is not None and (
            type(self.oscillation_period) is not int or not 2 <= self.oscillation_period <= 32
        ):
            raise ValueError("oscillation_period must be between 2 and 32")
        if type(self.oscillation_repeats) is not int or not 2 <= self.oscillation_repeats <= 8:
            raise ValueError("oscillation_repeats must be between 2 and 8")


@dataclass
class _StepState:
    retries: int = 0
    retries_in_flight: int = 0
    retry_sources: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RETRY_SOURCES, 0))
    history: deque = field(default_factory=deque)
    pending: dict[str, Any] = field(default_factory=dict)
    last_status: str | None = None
    last_decision_id: str | None = None
    last_mutating: bool | None = None
    last_arguments: str | None = None
    active_calls: int = 0
    mutating_in_flight: int = 0
    semantic_epoch: int = 0


class _LoopGuard:
    def __init__(self, limits: LoopLimits) -> None:
        self.limits = limits
        self.window = max(
            limits.max_identical_calls or 1,
            (limits.oscillation_period or 1) * limits.oscillation_repeats,
        )

    def _feedback(
        self,
        context: HookContext,
        state: _StepState,
        action: str,
        reason: str,
        message: str,
        retry_of: str | None = None,
    ) -> Decision:
        step = context.dispatch.step
        refs = []
        if step is not None:
            refs.append(f"step:{step.step_id}")
            if step.argument_fingerprint is not None:
                refs.append(f"arguments:sha256:{step.argument_fingerprint}")
            if step.progress_fingerprint is not None:
                refs.append(f"progress:sha256:{step.progress_fingerprint}")
        if context.dispatch.retry_source is not None:
            refs.append(f"retry_source:{context.dispatch.retry_source}")
        return Decision(
            action, reason, evidence_refs=tuple(refs), retry_of=retry_of, feedback=message
        )

    def _oscillating(self, history: list[tuple[str, str]]) -> bool:
        maximum = self.limits.oscillation_period
        if maximum is None:
            return False
        for period in range(2, maximum + 1):
            size = period * self.limits.oscillation_repeats
            if len(history) < size:
                continue
            tail = history[-size:]
            pattern = tail[:period]
            if len(set(pattern)) > 1 and all(
                tail[index] == pattern[index % period] for index in range(size)
            ):
                return True
        return False

    def __call__(self, context: HookContext) -> Decision:
        step = context.dispatch.step
        if step is None:
            if context.dispatch.retry_source is not None:
                return Decision(
                    "escalate",
                    "retry_step_unknown",
                    feedback="Retry admission needs an explicit step identity and declared safe semantics.",
                )
            return Decision(
                "continue",
                "loop_evidence_unavailable",
                feedback="No step identity was supplied; no semantic no-progress conclusion is available.",
            )
        states = context.state.setdefault("steps", {})
        key = (context.branch_id, step.step_id)
        state = states.get(key)
        if state is None:
            state = states[key] = _StepState(history=deque(maxlen=self.window))
        retry = context.dispatch.retry_source is not None
        if context.hook.phase == "after":
            pending = state.pending.pop(context.call_id, None)
            if pending is not None:
                if pending["admitted"]:
                    state.active_calls -= 1
                    if pending["mutating"] is True:
                        state.mutating_in_flight -= 1
                if pending["retry_reserved"]:
                    state.retries_in_flight -= 1
                if context.dispatched:
                    if pending["overlapped"] or pending["reset_history"]:
                        state.history.clear()
                        state.semantic_epoch += 1
                    if pending["retry"]:
                        state.retries += 1
                        state.retry_sources[pending["retry_source"]] += 1
                    if pending["history"] is not None and pending["epoch"] == state.semantic_epoch:
                        state.history.append(pending["history"])
                    state.last_status = context.status
                    state.last_decision_id = pending["decision_id"]
                    state.last_mutating = step.mutating
                    state.last_arguments = step.argument_fingerprint
            return self._feedback(
                context,
                state,
                "continue",
                "loop_attempt_recorded",
                "Attempt outcome recorded; retries remain host-controlled.",
            )

        prior = state.last_decision_id if retry else None
        action, reason = "continue", "loop_continue"
        message = "The declared step is within its configured operational limits."
        evidence = None
        reset_history = retry or step.polling
        overlapped = state.active_calls > 0
        retry_count = state.retries + state.retries_in_flight
        ambiguous_repeat = (
            not retry
            and state.last_status in {"error", "cancelled", "closed"}
            and step.argument_fingerprint is not None
            and step.argument_fingerprint == state.last_arguments
            and (step.mutating is not False or state.last_mutating is not False)
            and not step.retry_safe
        )
        if ambiguous_repeat:
            action, reason = "escalate", "ambiguous_side_effect_repeat"
            message = "Identical input follows an ambiguous potentially mutating attempt; explicit safe retry semantics are required."
        elif retry:
            mutating_in_flight = state.mutating_in_flight > 0
            safe = step.retry_safe or (
                step.mutating is False
                and state.last_mutating is not True
                and not mutating_in_flight
            )
            if not safe:
                action, reason = "escalate", "retry_safety_unknown"
                message = "Retrying a potentially mutating step requires declared safe retry semantics; prior side effects may be ambiguous."
            elif (
                self.limits.max_retries_per_step is not None
                and retry_count >= self.limits.max_retries_per_step
            ):
                action, reason = "stop", "step_retry_limit"
                message = "The step reached its configured retry limit; no additional attempt is admitted."
            else:
                message = f"Declared {context.dispatch.retry_source} retry admitted within its bound; no-progress heuristics are not applied to retries."
        elif step.polling:
            reason = "declared_polling"
            message = (
                "Declared polling bypasses semantic repetition guards; global budgets still apply."
            )
        elif step.argument_fingerprint is None or step.progress_fingerprint is None:
            reset_history = True
            reason = "loop_evidence_unavailable"
            message = "Input/progress evidence is incomplete; repeated names alone do not establish a no-progress loop."
        else:
            evidence = (step.argument_fingerprint, step.progress_fingerprint)
            if overlapped:
                evidence = None
                reset_history = True
                reason = "loop_concurrent_evidence"
                message = "Overlapping attempts do not establish a sequential no-progress cycle; global budgets still apply."
            history = list(state.history)
            repeated = 0
            for previous in reversed(history):
                if previous != evidence:
                    break
                repeated += 1
            if (
                evidence is not None
                and self.limits.max_identical_calls is not None
                and repeated >= self.limits.max_identical_calls
            ):
                action, reason = "stop", "identical_step_limit"
                message = f"Unchanged input/progress fingerprints reached the configured bound ({self.limits.max_identical_calls}); this is not proof of semantic uselessness."
            elif evidence is not None and self._oscillating(history + [evidence]):
                action, reason = "stop", "oscillation_limit"
                message = "Caller-declared input/progress fingerprints repeat a configured cycle; validate the task-specific meaning before changing the workflow."
        admitted = action == "continue" or context.mode == "shadow"
        retry_reserved = retry and admitted
        if retry_reserved:
            state.retries_in_flight += 1
        if admitted:
            state.active_calls += 1
            if step.mutating is True:
                state.mutating_in_flight += 1
        state.pending[context.call_id] = {
            "retry": retry,
            "retry_source": context.dispatch.retry_source,
            "retry_reserved": retry_reserved,
            "history": evidence if admitted else None,
            "decision_id": context.decision_id,
            "admitted": admitted,
            "reset_history": reset_history,
            "overlapped": overlapped,
            "mutating": step.mutating,
            "epoch": state.semantic_epoch,
        }
        return self._feedback(context, state, action, reason, message, retry_of=prior)


def loop_guard_policy(
    limits: LoopLimits = LoopLimits(),
    *,
    policy_id: str = "loop_guard",
    priority: int = 200,
    boundaries: Iterable[str] = BOUNDARIES,
) -> Policy:
    """Compose with budget_policy for total iteration/retry/resource admission."""
    if type(limits) is not LoopLimits:
        raise ValueError("limits must be LoopLimits")
    try:
        selected = frozenset(boundaries)
    except TypeError:
        raise ValueError("loop boundaries must be named hooks") from None
    if not selected or not selected <= BOUNDARIES:
        raise ValueError("unsupported loop boundary")
    return Policy(
        policy_id,
        "1.0",
        _LoopGuard(limits),
        hooks=frozenset(
            Hook(boundary, phase) for boundary in selected for phase in ("before", "after")
        ),
        actions=frozenset({"continue", "stop", "escalate"}),
        priority=priority,
        configuration={
            "limits": asdict(limits),
            "automatic_retries": False,
            "semantic_progress_source": "caller_declared",
            "history_bounded": True,
            "boundaries": sorted(selected),
        },
    )
