"""Private, versioned JSON codecs for quiescent built-in policy state.

This is operational state supplied by a trusted host store, never trace replay
or a general-purpose object deserializer. Unknown policies are not resumable.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import fields
from fractions import Fraction

from agentloop.budget_types import COUNT_KEYS, RETRY_SOURCES
from agentloop.budgets import _BudgetEvaluator, _State
from agentloop.context_types import canonical, fingerprint
from agentloop.harness_evidence import valid_decision_id
from agentloop.loop_guards import _LoopGuard, _StepState

_REASONS = frozenset(
    "invalid_policy_state in_flight_work enforcement_required budget_policy_required "
    "unsupported_policy_state invalid_clock_domain stopped_run state_size_limit "
    "incompatible_schema incompatible_configuration invalid_run_lineage "
    "incompatible_clock_domain invalid_store_limit invalid_owner stale_checkpoint "
    "claim_required missing_checkpoint checkpoint_owner_mismatch invalid_request_id "
    "checkpoint_changed invalid_recovery_evidence unsupported_recovery_adapter "
    "checkpoint_provider_required unsupported_recovery_store invalid_retry_safety "
    "invalid_checkpoint subgraph_recovery_unsupported recovery_already_running "
    "missing_run run_lineage_changed run_checkpoint_mismatch completed_checkpoint "
    "invalid_checkpoint_reference dynamic_interrupt_requires_host "
    "side_effect_reconciliation_required new_trace_required duplicate_or_stale_resume "
    "synchronous_durability_required".split()
)


class CheckpointError(ValueError):
    def __init__(self, reason):
        if not isinstance(reason, str) or reason not in _REASONS:
            reason = "checkpoint_error"
        self.reason_code = reason
        super().__init__("Checkpoint recovery failed: " + reason)


def _require(condition, reason="invalid_policy_state"):
    if not condition:
        raise CheckpointError(reason)


def _counts(value, keys):
    return (
        type(value) is dict
        and set(value) == set(keys)
        and all(type(item) is int and item >= 0 for item in value.values())
    )


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 4096


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _budget_state(state):
    _require(type(state) is _State and _text(state.scope_id))
    _require(type(state.pending) is dict and not state.pending, "in_flight_work")
    for name in ("committed", "refunded", "reserved_counts"):
        _require(_counts(getattr(state, name), COUNT_KEYS))
    _require(_counts(state.retries, RETRY_SOURCES))
    _require(not any(state.reserved_counts.values()), "in_flight_work")
    for name in (
        "metered_calls",
        "tokens_known",
        "tokens_held",
        "unknown_calls",
        "unknown_tokens",
        "unknown_cost",
        "tokens_refunded",
        "reserved_metered",
        "reserved_tokens",
        "reserved_unknown_tokens",
        "reserved_unknown_cost",
    ):
        value = getattr(state, name)
        _require(type(value) is int and value >= 0)
        if name.startswith("reserved_"):
            _require(value == 0, "in_flight_work")
    for name in ("cost_known", "cost_held", "cost_refunded", "reserved_cost"):
        _require(type(getattr(state, name)) is Fraction and getattr(state, name) >= 0)
    _require(state.reserved_cost == 0, "in_flight_work")
    _require(
        state.last_clock is None
        or (
            type(state.last_clock) in {int, float}
            and math.isfinite(state.last_clock)
            and state.last_clock >= 0
        )
    )
    _require(
        type(state.seen_usage) is dict
        and all(_text(key) and isinstance(value, str) for key, value in state.seen_usage.items())
    )


def _step_state(state, window):
    _require(type(state) is _StepState)
    _require(type(state.pending) is dict and not state.pending, "in_flight_work")
    for name in (
        "retries",
        "retries_in_flight",
        "active_calls",
        "mutating_in_flight",
        "semantic_epoch",
    ):
        value = getattr(state, name)
        _require(type(value) is int and value >= 0)
    _require(
        state.retries_in_flight == state.active_calls == state.mutating_in_flight == 0,
        "in_flight_work",
    )
    _require(_counts(state.retry_sources, RETRY_SOURCES))
    _require(
        type(state.history) is deque
        and state.history.maxlen == window
        and len(state.history) <= window
    )
    _require(
        all(
            type(pair) is tuple and len(pair) == 2 and all(_digest(value) for value in pair)
            for pair in state.history
        )
    )
    _require(
        state.last_status is None
        or state.last_status in {"ok", "error", "cancelled", "closed", "denied"}
    )
    _require(state.last_decision_id is None or valid_decision_id(state.last_decision_id))
    _require(state.last_mutating is None or type(state.last_mutating) is bool)
    _require(state.last_arguments is None or _digest(state.last_arguments))


def validate_states(harness, states):
    _require(harness.config.mode == "enforce", "enforcement_required")
    policies = {policy.policy_id: policy for policy in harness.config.policies}
    _require(type(states) is dict and set(states) <= set(policies))
    _require(
        any(type(policy.evaluate) is _BudgetEvaluator for policy in policies.values()),
        "budget_policy_required",
    )
    for policy in policies.values():
        evaluator = policy.evaluate
        _require(type(evaluator) in {_BudgetEvaluator, _LoopGuard}, "unsupported_policy_state")
        value = states.get(policy.policy_id, {})
        _require(type(value) is dict)
        if not value:
            continue
        if type(evaluator) is _BudgetEvaluator:
            _require(set(value) == {"budget"})
            _budget_state(value["budget"])
        else:
            _require(set(value) == {"steps"} and type(value["steps"]) is dict)
            for key, state in value["steps"].items():
                _require(type(key) is tuple and len(key) == 2 and all(_text(item) for item in key))
                _step_state(state, evaluator.window)


def _encode(value):
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if type(value) is Fraction:
        return {"fraction": [value.numerator, value.denominator]}
    if type(value) is dict:
        return {"map": [[_encode(key), _encode(item)] for key, item in value.items()]}
    if type(value) is tuple:
        return {"tuple": [_encode(item) for item in value]}
    if type(value) is deque:
        return {"deque": [_encode(item) for item in value], "maxlen": value.maxlen}
    if type(value) in {_State, _StepState}:
        return {
            "type": "budget" if type(value) is _State else "loop",
            "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    raise CheckpointError("unsupported_policy_state")


def _decode(value):
    if value is None or type(value) in {bool, int, float, str}:
        return value
    _require(type(value) is dict)
    if set(value) == {"fraction"}:
        pair = value["fraction"]
        _require(
            type(pair) is list
            and len(pair) == 2
            and all(type(item) is int for item in pair)
            and pair[1] > 0
        )
        return Fraction(*pair)
    if set(value) == {"map"}:
        _require(type(value["map"]) is list)
        result = {}
        for pair in value["map"]:
            _require(type(pair) is list and len(pair) == 2)
            key = _decode(pair[0])
            _require(type(key) in {str, tuple} and key not in result)
            result[key] = _decode(pair[1])
        return result
    if set(value) == {"tuple"}:
        _require(type(value["tuple"]) is list)
        return tuple(_decode(item) for item in value["tuple"])
    if set(value) == {"deque", "maxlen"}:
        _require(
            type(value["deque"]) is list
            and type(value["maxlen"]) is int
            and 1 <= value["maxlen"] <= 256
            and len(value["deque"]) <= value["maxlen"]
        )
        return deque((_decode(item) for item in value["deque"]), maxlen=value["maxlen"])
    if set(value) == {"type", "fields"}:
        cls = {"budget": _State, "loop": _StepState}.get(value["type"])
        _require(
            cls is not None
            and type(value["fields"]) is dict
            and set(value["fields"]) == {field.name for field in fields(cls)}
        )
        return cls(**{name: _decode(item) for name, item in value["fields"].items()})
    raise CheckpointError("invalid_policy_state")


def _capabilities(harness):
    capabilities = harness.capabilities
    return fingerprint(
        {
            "name": capabilities.name,
            "hooks": sorted((hook.boundary, hook.phase) for hook in capabilities.hooks),
            "actions": sorted(capabilities.actions),
            "execution_kinds": sorted(capabilities.execution_kinds),
        }
    )


def seal_run(run, *, clock_domain):
    """Atomically retire a quiescent attempt and export its actual policy state."""
    _require(_text(clock_domain), "invalid_clock_domain")
    with run._lock:
        _require(not run._stopped, "stopped_run")
        before = {result.call_id for result in run._results if result.hook.phase == "before"}
        after = {result.call_id for result in run._results if result.hook.phase == "after"}
        _require(before == after, "in_flight_work")
        validate_states(run.harness, run._states)
        payload = {
            "schema_version": "1.0",
            "config_hash": run.harness.config.config_hash,
            "adapter": _capabilities(run.harness),
            "run_id": run.run_id,
            "clock_domain": clock_domain,
            "states": _encode(run._states),
        }
        encoded = canonical(payload)
        _require(len(encoded.encode("utf-8")) <= 4_000_000, "state_size_limit")
        run._stopped = True
        return json.loads(encoded)


def restore_run(harness, payload, *, clock_domain, run_id):
    """Restore only a trusted, claimed host record under identical configuration."""
    try:
        _require(
            type(payload) is dict
            and set(payload)
            == {"schema_version", "config_hash", "adapter", "run_id", "clock_domain", "states"}
        )
        _require(payload["schema_version"] == "1.0", "incompatible_schema")
        _require(_text(payload["clock_domain"]) and _text(clock_domain), "invalid_clock_domain")
        _require(
            payload["config_hash"] == harness.config.config_hash
            and payload["adapter"] == _capabilities(harness),
            "incompatible_configuration",
        )
        _require(
            _text(payload["run_id"]) and _text(run_id) and payload["run_id"] != run_id,
            "invalid_run_lineage",
        )
        _require(len(canonical(payload).encode("utf-8")) <= 4_000_000, "state_size_limit")
        # Absolute monotonic deadlines cannot safely move between clock epochs.
        if any(
            type(policy.evaluate) is _BudgetEvaluator
            and policy.evaluate.limits.deadline_at is not None
            for policy in harness.config.policies
        ):
            _require(payload["clock_domain"] == clock_domain, "incompatible_clock_domain")
        states = _decode(payload["states"])
        validate_states(harness, states)
    except CheckpointError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError):
        raise CheckpointError("invalid_policy_state") from None
    run = harness.start_run(run_id)
    run._states = states
    return run
