"""Atomic admission budgets for explicitly supported Python dispatch boundaries."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any

from agentloop.budget_types import (
    COUNT_KEYS,
    RETRY_SOURCES,
    BudgetSnapshot,
    BudgetValidationError,
    Reservation,
    amount,
    count,
    money_text,
    number,
)
from agentloop.harness import BOUNDARIES, Decision, Hook, HookContext, Policy, _callable_kind

_COUNT_BOUNDARY = {"model": "model_calls", "tool": "tool_calls", "iteration": "iterations"}


@dataclass(frozen=True)
class BudgetLimits:
    """Optional per-run limits; deadline_at is an absolute monotonic-clock value."""

    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_iterations: int | None = None
    max_retries: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        for key in (*COUNT_KEYS, "tokens"):
            count(getattr(self, f"max_{key}"), key)
        number(self.max_cost_usd, "max_cost_usd")
        number(self.deadline_at, "deadline_at")


@dataclass
class _Pending:
    counts: dict[str, int]
    metered: bool
    tokens: int | None
    cost: Fraction | None
    accounted: bool


@dataclass
class _State:
    scope_id: str
    committed: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    refunded: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    retries: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RETRY_SOURCES, 0))
    pending: dict[str, _Pending] = field(default_factory=dict)
    seen_usage: dict[str, str] = field(default_factory=dict)
    metered_calls: int = 0
    tokens_known: int = 0
    cost_known: Fraction = Fraction(0)
    tokens_held: int = 0
    cost_held: Fraction = Fraction(0)
    unknown_calls: int = 0
    unknown_tokens: int = 0
    unknown_cost: int = 0
    tokens_refunded: int = 0
    cost_refunded: Fraction = Fraction(0)
    last_clock: float | None = None
    reserved_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    reserved_metered: int = 0
    reserved_tokens: int = 0
    reserved_cost: Fraction = Fraction(0)
    reserved_unknown_tokens: int = 0
    reserved_unknown_cost: int = 0


class _BudgetEvaluator:
    def __init__(
        self,
        limits: BudgetLimits,
        unknown_usage: str,
        soft_fraction: float | None,
        metered: frozenset[str],
        clock: Callable[[], float],
    ) -> None:
        self.limits = limits
        self.unknown_usage = unknown_usage
        self.soft_fraction = None if soft_fraction is None else Fraction(str(soft_fraction))
        self.metered = metered
        self.clock = clock
        self.cost_limit = None if limits.max_cost_usd is None else amount(limits.max_cost_usd)

    def _now(self, state: _State) -> float | None:
        if self.limits.deadline_at is None:
            return None
        try:
            reading = self.clock()
        except Exception:
            raise BudgetValidationError("monotonic clock could not be read") from None
        if inspect.iscoroutine(reading):
            reading.close()
        now = number(reading, "monotonic clock", optional=False)
        if state.last_clock is not None and now < state.last_clock:
            raise BudgetValidationError("monotonic clock moved backwards")
        state.last_clock = now
        return now

    def _pending(self, context: HookContext, *, accounted: bool) -> _Pending:
        counts = dict.fromkeys(COUNT_KEYS, 0)
        if context.hook.boundary in _COUNT_BOUNDARY:
            counts[_COUNT_BOUNDARY[context.hook.boundary]] = 1
        if context.dispatch.retry_source is not None:
            counts["retries"] = 1
        metered = context.hook.boundary in self.metered
        reservation = context.dispatch.reservation or Reservation()
        return _Pending(
            counts,
            metered,
            reservation.tokens if reservation.tokens_known else None if metered else 0,
            amount(reservation.cost_usd)
            if reservation.cost_known
            else None
            if metered
            else Fraction(0),
            accounted,
        )

    def _in_flight(self, state: _State) -> dict[str, Any]:
        return {
            **state.reserved_counts,
            "metered_calls": state.reserved_metered,
            "tokens_reserved": state.reserved_tokens,
            "cost_reserved_usd": state.reserved_cost,
            "unknown_token_reservations": state.reserved_unknown_tokens,
            "unknown_cost_reservations": state.reserved_unknown_cost,
        }

    def _reserve(self, state: _State, pending: _Pending, direction: int) -> None:
        if not pending.accounted:
            return
        for key, value in pending.counts.items():
            state.reserved_counts[key] += direction * value
        if pending.metered:
            state.reserved_metered += direction
            state.reserved_tokens += direction * (pending.tokens or 0)
            state.reserved_cost += direction * (pending.cost or Fraction(0))
            state.reserved_unknown_tokens += direction * (pending.tokens is None)
            state.reserved_unknown_cost += direction * (pending.cost is None)

    def _used(self, state: _State) -> dict[str, Any]:
        inflight = self._in_flight(state)
        return {
            **{key: state.committed[key] + inflight[key] for key in COUNT_KEYS},
            "tokens": state.tokens_known + state.tokens_held + inflight["tokens_reserved"],
            "cost_usd": state.cost_known + state.cost_held + inflight["cost_reserved_usd"],
        }

    def _limits(self) -> dict[str, Any]:
        return {
            **{key: getattr(self.limits, f"max_{key}") for key in (*COUNT_KEYS, "tokens")},
            "cost_usd": self.cost_limit,
        }

    def _soft(self, state: _State) -> list[str]:
        if self.soft_fraction is None:
            return []
        used = self._used(state)
        return sorted(
            key
            for key, limit in self._limits().items()
            if limit is not None and used[key] >= limit * self.soft_fraction
        )

    def _snapshot(
        self, context: HookContext, state: _State, codes: set[str], now: float | None
    ) -> BudgetSnapshot:
        inflight = self._in_flight(state)
        inflight["cost_reserved_usd"] = money_text(inflight["cost_reserved_usd"])
        limits = self._limits()
        limits["cost_usd"] = None if self.cost_limit is None else money_text(self.cost_limit)
        limits["deadline_at"] = self.limits.deadline_at
        tokens_total = None if state.unknown_tokens or not self.metered else state.tokens_known
        cost_total = (
            None if state.unknown_cost or not self.metered else money_text(state.cost_known)
        )
        token_overshoot = (
            None
            if tokens_total is None or self.limits.max_tokens is None
            else max(0, tokens_total - self.limits.max_tokens)
        )
        cost_overshoot = (
            None
            if cost_total is None or self.cost_limit is None
            else money_text(max(Fraction(0), state.cost_known - self.cost_limit))
        )
        return BudgetSnapshot.from_dict(
            {
                "schema_version": "1.0",
                "budget_scope_id": state.scope_id,
                "scope": "wrapped_dispatch",
                "hard_spend_cap": False,
                "spend_enforcement": "not_configured"
                if self.cost_limit is None
                else "shadow"
                if context.mode == "shadow"
                else "best_effort",
                "token_enforcement": "not_configured"
                if self.limits.max_tokens is None
                else "shadow"
                if context.mode == "shadow"
                else "best_effort",
                "possible_spend_overshoot": None if self.cost_limit is None else True,
                "unknown_usage_policy": self.unknown_usage,
                "soft_fraction": None if self.soft_fraction is None else float(self.soft_fraction),
                "deadline_enforcement": "cooperative_admission",
                "metered_boundaries": sorted(self.metered),
                "limits": limits,
                "committed": {
                    **state.committed,
                    "metered_calls": state.metered_calls,
                    "tokens_known": state.tokens_known,
                    "cost_known_usd": money_text(state.cost_known),
                },
                "in_flight": inflight,
                "refunded": {
                    **state.refunded,
                    "tokens_bound": state.tokens_refunded,
                    "cost_bound_usd": money_text(state.cost_refunded),
                },
                "unknown_usage": {
                    "calls": state.unknown_calls,
                    "tokens": state.unknown_tokens,
                    "cost": state.unknown_cost,
                    "tokens_held": state.tokens_held,
                    "cost_held_usd": money_text(state.cost_held),
                },
                "total_usage": {"tokens": tokens_total, "cost_usd": cost_total},
                "retry_sources": dict(state.retries),
                "status_codes": sorted(codes or {"ok"}),
                "soft_exceeded": self._soft(state),
                "overshoot": {"tokens": token_overshoot, "cost_usd": cost_overshoot},
                "deadline_remaining_s": None
                if self.limits.deadline_at is None or now is None
                else max(0.0, self.limits.deadline_at - now),
                "last_usage": None if context.usage is None else context.usage.to_dict(),
                "usage_error": context.usage_error,
                "reservation": None
                if context.dispatch.reservation is None
                else context.dispatch.reservation.to_dict(),
            }
        )

    def _decision(
        self,
        context: HookContext,
        state: _State,
        action: str,
        reason: str,
        codes: set[str],
        now: float | None,
    ) -> Decision:
        refs = (
            list(context.dispatch.reservation.evidence_refs) if context.dispatch.reservation else []
        )
        if context.usage is not None and context.usage.usage_id is not None:
            refs.append(context.usage.usage_id)
        return Decision(
            action,
            reason,
            evidence_refs=tuple(refs),
            budget_snapshot=self._snapshot(context, state, codes, now),
        )

    def _before(self, context: HookContext, state: _State, now: float | None) -> Decision:
        pending = self._pending(context, accounted=False)
        used = self._used(state)
        requested = {
            **pending.counts,
            "tokens": (pending.tokens or 0) if pending.metered else 0,
            "cost_usd": (pending.cost or Fraction(0)) if pending.metered else Fraction(0),
        }
        codes = {
            f"limit_{key}"
            for key, limit in self._limits().items()
            if limit is not None and used[key] + requested[key] > limit
        }
        inflight = self._in_flight(state)
        unknown = set()
        if self.limits.max_tokens is not None and (
            state.unknown_tokens
            or inflight["unknown_token_reservations"]
            or (pending.metered and pending.tokens is None)
        ):
            unknown.add("unknown_tokens")
        if self.cost_limit is not None and (
            state.unknown_cost
            or inflight["unknown_cost_reservations"]
            or (pending.metered and pending.cost is None)
        ):
            unknown.add("unknown_cost")
        clock_invalid = self.limits.deadline_at is not None and now is None
        deadline = (
            self.limits.deadline_at is not None
            and now is not None
            and now >= self.limits.deadline_at
        )
        if clock_invalid:
            codes.add("clock_invalid")
        if deadline:
            codes.add("deadline_expired")
        limited = bool(codes)
        codes |= unknown
        if clock_invalid:
            action, reason = "escalate", "budget_clock_invalid"
        elif deadline:
            action, reason = "stop", "budget_deadline"
        elif unknown and self.unknown_usage == "escalate":
            action, reason = "escalate", "budget_usage_unknown"
        elif limited:
            action, reason = "deny", "budget_limit"
        elif unknown and self.unknown_usage == "deny":
            action, reason = "deny", "budget_usage_unknown"
        else:
            action, reason = "continue", "budget_monitor_unknown" if unknown else "budget_available"
        # Enforced denials do not occupy capacity. Shadow still tracks the work
        # that will actually run, even when its proposed admission was denied.
        pending.accounted = action == "continue" or context.mode == "shadow"
        if context.call_id in state.pending:
            raise BudgetValidationError("duplicate budget admission")
        state.pending[context.call_id] = pending
        self._reserve(state, pending, 1)
        if action == "continue" and self._soft(state):
            reason = "budget_soft_limit"
        return self._decision(context, state, action, reason, codes, now)

    def _refund(self, state: _State, pending: _Pending) -> None:
        if not pending.accounted:
            return
        for key, value in pending.counts.items():
            state.refunded[key] += value
        if pending.metered:
            state.tokens_refunded += pending.tokens or 0
            state.cost_refunded += pending.cost or Fraction(0)

    def _after(self, context: HookContext, state: _State, now: float | None) -> Decision:
        pending = state.pending.pop(context.call_id, None)
        if pending is None:
            pending = self._pending(context, accounted=False)
        else:
            self._reserve(state, pending, -1)
        clock_invalid = self.limits.deadline_at is not None and now is None
        if not context.dispatched:
            self._refund(state, pending)
            return self._decision(
                context,
                state,
                "continue",
                "budget_not_dispatched",
                {"not_admitted"} | ({"clock_invalid"} if clock_invalid else set()),
                now,
            )
        for key, value in pending.counts.items():
            state.committed[key] += value
        if context.dispatch.retry_source is not None:
            state.retries[context.dispatch.retry_source] += 1
        codes: set[str] = set()
        unknown: set[str] = set()
        bound_exceeded = False
        if pending.metered:
            state.metered_calls += 1
            usage = context.usage
            tokens_known = usage is not None and usage.tokens_known and context.usage_error is None
            cost_known = usage is not None and usage.cost_known and context.usage_error is None
            duplicate = False
            if (
                usage is not None
                and usage.complete
                and usage.exclusive
                and context.usage_error is None
                and usage.usage_id is not None
                and (tokens_known or cost_known)
            ):
                serialized = json.dumps(usage.to_dict(), sort_keys=True, separators=(",", ":"))
                previous = state.seen_usage.get(usage.usage_id)
                if previous is None:
                    state.seen_usage[usage.usage_id] = serialized
                elif previous == serialized:
                    duplicate = True
                    tokens_known = cost_known = True
                    codes.add("duplicate_usage")
                else:
                    tokens_known = cost_known = False
                    codes.add("usage_conflict")
            if context.usage_error is not None:
                codes.add("usage_collector_error")
            if tokens_known:
                actual_tokens = 0 if duplicate else usage.tokens
                state.tokens_known += actual_tokens
                if pending.tokens is not None:
                    if pending.accounted:
                        state.tokens_refunded += max(0, pending.tokens - actual_tokens)
                    if actual_tokens > pending.tokens and self.limits.max_tokens is not None:
                        bound_exceeded = True
            else:
                state.unknown_tokens += 1
                state.tokens_held += pending.tokens or 0
                codes.add("unknown_tokens")
                if self.limits.max_tokens is not None:
                    unknown.add("tokens")
            if cost_known:
                actual_cost = Fraction(0) if duplicate else amount(usage.cost_usd)
                state.cost_known += actual_cost
                if pending.cost is not None:
                    if pending.accounted:
                        state.cost_refunded += max(Fraction(0), pending.cost - actual_cost)
                    if actual_cost > pending.cost and self.cost_limit is not None:
                        bound_exceeded = True
            else:
                state.unknown_cost += 1
                state.cost_held += pending.cost or Fraction(0)
                codes.add("unknown_cost")
                if self.cost_limit is not None:
                    unknown.add("cost")
            if not tokens_known or not cost_known:
                state.unknown_calls += 1
        used = self._used(state)
        exceeded = {
            f"limit_{key}"
            for key, limit in self._limits().items()
            if limit is not None and used[key] > limit
        }
        codes |= exceeded
        if bound_exceeded:
            codes.add("bound_exceeded")
        if clock_invalid:
            codes.add("clock_invalid")
            action, reason = "escalate", "budget_clock_invalid"
        elif bound_exceeded:
            action, reason = "escalate", "budget_bound_exceeded"
        elif unknown and self.unknown_usage == "escalate":
            action, reason = "escalate", "budget_usage_unknown"
        elif exceeded or (unknown and self.unknown_usage == "deny"):
            action, reason = "stop", "budget_limit" if exceeded else "budget_usage_unknown"
        else:
            action, reason = (
                "continue",
                "budget_monitor_unknown" if unknown else "budget_reconciled",
            )
        if (
            self.limits.deadline_at is not None
            and now is not None
            and now >= self.limits.deadline_at
        ):
            codes.add("deadline_expired")
        return self._decision(context, state, action, reason, codes, now)

    def __call__(self, context: HookContext) -> Decision:
        state = context.state.get("budget")
        if state is None:
            state = context.state["budget"] = _State(scope_id=f"budget:{context.decision_id}")
        try:
            now = self._now(state)
        except BudgetValidationError:
            now = None
        return (
            self._before(context, state, now)
            if context.hook.phase == "before"
            else self._after(context, state, now)
        )


def budget_policy(
    limits: BudgetLimits,
    *,
    unknown_usage: str = "deny",
    soft_fraction: float | None = None,
    boundaries: Iterable[str] = BOUNDARIES,
    metered_boundaries: Iterable[str] = ("model",),
    clock: Callable[[], float] = time.monotonic,
    policy_id: str = "budget",
    priority: int = 100,
) -> Policy:
    """Build a reusable policy whose counters and reservations remain run-local.

    Monetary amounts use exact arithmetic over their declared decimal values. This
    does not make declared bounds, calculated prices, or adapter coverage a hard
    spend cap. Deadlines only stop new admissions and never kill in-flight work.
    """
    if type(limits) is not BudgetLimits:
        raise BudgetValidationError("limits must be BudgetLimits")
    if not isinstance(unknown_usage, str) or unknown_usage not in {
        "deny",
        "escalate",
        "monitor_only",
    }:
        raise BudgetValidationError("unknown_usage must be deny, escalate, or monitor_only")
    soft = number(soft_fraction, "soft_fraction")
    if soft is not None and soft > 1:
        raise BudgetValidationError("soft_fraction must be between zero and one")
    try:
        selected, metered = frozenset(boundaries), frozenset(metered_boundaries)
    except TypeError:
        raise BudgetValidationError("budget boundaries must be named hooks") from None
    if not selected or not selected <= BOUNDARIES or not metered <= {"model", "tool"}:
        raise BudgetValidationError("unsupported budget boundary")
    metered &= selected
    for boundary, name in _COUNT_BOUNDARY.items():
        if getattr(limits, f"max_{name}") is not None and boundary not in selected:
            raise BudgetValidationError("a configured call limit lacks its dispatch boundary")
    if (limits.max_tokens is not None or limits.max_cost_usd is not None) and not metered:
        raise BudgetValidationError("token/cost limits require a metered boundary")
    if not callable(clock) or _callable_kind(clock) != "sync":
        raise BudgetValidationError("clock must be a synchronous callable")
    return Policy(
        policy_id,
        "1.0",
        _BudgetEvaluator(limits, unknown_usage, soft, metered, clock),
        hooks=frozenset(
            Hook(boundary, phase) for boundary in selected for phase in ("before", "after")
        ),
        actions=frozenset({"continue", "deny", "stop", "escalate"}),
        priority=priority,
        configuration={
            "limits": asdict(limits),
            "unknown_usage": unknown_usage,
            "soft_fraction": soft,
            "boundaries": sorted(selected),
            "metered_boundaries": sorted(metered),
            "hard_spend_cap": False,
            "deadline_enforcement": "cooperative_admission",
        },
    )
