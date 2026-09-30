"""Explicit remove/compress candidates on the generic experiment contract."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from fractions import Fraction

from agentloop.budget_types import Reservation, ResourceUsage
from agentloop.context_types import EXACT_COUNTS, ContextTokenCount, identifier, synchronous
from agentloop.experiment_observations import observation, record_experiment_observations
from agentloop.experiment_types import (
    ExperimentResult,
    ExperimentRunner,
    canonical,
    fingerprint,
    reference,
)
from agentloop.tracer import current_trace

CONTEXT_EXPERIMENT_KEY = "agentloop.context_experiment"


def _path(value):
    if (
        not isinstance(value, (list, tuple))
        or not 1 <= len(value) <= 32
        or any(
            not (isinstance(part, str) and 0 < len(part) <= 256 or type(part) is int and part >= 0)
            for part in value
        )
    ):
        raise ValueError("context paths require bounded string keys or nonnegative indexes")
    return tuple(value)


def _overlap(left, right):
    return left[: len(right)] == right or right[: len(left)] == left


def _value(payload, path):
    value = payload
    for part in path:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, list) and type(part) is int and part < len(value):
            value = value[part]
        else:
            raise ValueError("context selection is missing or incompatible")
    return value


def _deadline(request):
    if request.remaining_s <= 0:
        raise TimeoutError("experiment template deadline expired")


def _count_kind(count):
    return (
        "observed"
        if count.provenance in EXACT_COUNTS
        else "estimated"
        if count.provenance == "estimated_words"
        else "declared"
    )


def _close_invalid(value):
    if inspect.iscoroutine(value) or inspect.isgenerator(value):
        value.close()


@dataclass(frozen=True)
class ContextSelection:
    selection_id: str
    path: tuple[str | int, ...]
    source_ref: str
    action: str = "remove"

    def __post_init__(self):
        identifier(self.selection_id, "selection_id")
        reference(self.source_ref, "selection reference")
        object.__setattr__(self, "path", _path(self.path))
        if self.action not in {"remove", "compress"}:
            raise ValueError("context action must be remove or compress")


def _sum_usage(usages):
    if len(usages) == 1:
        return usages[0]
    ids = [value.usage_id for value in usages if value.usage_id is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("compression and backend usage must be exclusive")
    complete = all(value.complete and value.exclusive for value in usages)
    tokens = (
        sum(value.tokens for value in usages)
        if all(value.tokens_known for value in usages)
        else None
    )
    cost = (
        float(sum((Fraction(str(value.cost_usd)) for value in usages), Fraction(0)))
        if all(value.cost_known for value in usages)
        else None
    )
    return ResourceUsage(
        tokens=tokens,
        cost_usd=cost,
        token_provenance="user_supplied" if tokens is not None else "unavailable",
        cost_provenance="user_reported" if cost is not None else "unavailable",
        complete=complete,
    )


def context_reduction_runner(
    candidate_id,
    backend,
    *,
    selections,
    protected_paths=(),
    token_counter=None,
    counter_ref=None,
    compressor=None,
    compressor_ref=None,
    on_missing="error",
    reservation=None,
):
    if (
        type(backend) is not ExperimentRunner
        or not isinstance(selections, (tuple, list))
        or not 1 <= len(selections) <= 64
        or any(type(item) is not ContextSelection for item in selections)
    ):
        raise ValueError("context experiments require an explicit backend and selections")
    selections = tuple(selections)
    if not isinstance(protected_paths, (list, tuple)) or len(protected_paths) > 64:
        raise ValueError("protected paths must be a bounded sequence")
    protected = tuple(_path(path) for path in protected_paths)
    if len(protected) > 64 or len({item.selection_id for item in selections}) != len(selections):
        raise ValueError("context declarations must be unique and bounded")
    if any(
        _overlap(item.path, other.path)
        for index, item in enumerate(selections)
        for other in selections[index + 1 :]
    ) or any(_overlap(item.path, path) for item in selections for path in protected):
        raise ValueError("selected paths overlap each other or protected context")
    compressing = any(item.action == "compress" for item in selections)
    if compressing and (not synchronous(compressor) or compressor_ref is None):
        raise ValueError("compression requires a trusted explicit callback/reference")
    if compressor_ref is not None:
        reference(compressor_ref, "compressor_ref")
    if token_counter is not None and (not synchronous(token_counter) or counter_ref is None):
        raise ValueError("token counter requires a trusted callback and reference")
    if counter_ref is not None:
        identifier(counter_ref, "counter_ref")
    if on_missing not in {"error", "keep_original"}:
        raise ValueError("unknown missing-selection policy")
    config = {
        "schema_version": "1.0",
        "backend": backend.declaration(),
        "selections": [asdict(item) for item in selections],
        "protected_paths": protected,
        "counter_ref": counter_ref,
        "compressor_ref": compressor_ref,
        "on_missing": on_missing,
    }
    config_hash = fingerprint(config)
    source = "context-config:" + config_hash

    def count(payload):
        if token_counter is None:
            return ContextTokenCount()
        result = token_counter(deepcopy(payload))
        if type(result) is not ContextTokenCount or result.reference not in {None, counter_ref}:
            _close_invalid(result)
            raise ValueError("token counter result/reference differs from declaration")
        return result

    def invoke(request):
        if backend.declaration() != config["backend"]:
            raise ValueError("context backend binding changed after configuration")
        before = request.inputs
        trace = current_trace()
        if trace is None:
            raise ValueError("context candidate requires an active experiment")
        receipt = {
            "schema_version": "1.0",
            "config_hash": config_hash,
            "input_hash": fingerprint(before),
            "selections": [],
            "state": "started",
            "output_input_hash": None,
            "compression_usage": [],
        }
        trace.metadata[CONTEXT_EXPERIMENT_KEY] = receipt
        usages = []
        try:
            _deadline(request)
            before_count = count(before)
            after = deepcopy(before)
            try:
                values = {item.selection_id: _value(before, item.path) for item in selections}
            except ValueError:
                receipt["state"] = "selection_missing"
                record_experiment_observations(
                    {
                        "context_applied": observation(False, unit="boolean", source_ref=source),
                        "context_state": observation(
                            "selection_missing", unit="state", source_ref=source
                        ),
                    }
                )
                if on_missing == "error":
                    raise
                _deadline(request)
                return backend.invoke(request)
            ordered = sorted(
                selections,
                key=lambda item: tuple(
                    (0, part) if type(part) is int else (1, part) for part in item.path
                ),
                reverse=True,
            )
            for item in ordered:
                value = values[item.selection_id]
                parent = _value(after, item.path[:-1]) if len(item.path) > 1 else after
                record = {
                    "selection_id": item.selection_id,
                    "source_ref": item.source_ref,
                    "path_hash": fingerprint(item.path),
                    "source_hash": fingerprint(value),
                    "action": item.action,
                    "replacement_hash": None,
                }
                receipt["selections"].append(record)
                if item.action == "remove":
                    del parent[item.path[-1]]
                else:
                    _deadline(request)
                    replacement = compressor(deepcopy(value), request)
                    if type(replacement) is not ExperimentResult:
                        _close_invalid(replacement)
                        raise ValueError("compressor must return ExperimentResult with usage")
                    parent[item.path[-1]] = replacement.output
                    record["replacement_hash"] = fingerprint(replacement.output)
                    usages.append(replacement.usage)
                    receipt["compression_usage"].append(replacement.usage.to_dict())
            _deadline(request)
            after_count = count(after)
            same_basis = (
                before_count.reference == after_count.reference
                and before_count.provenance == after_count.provenance
            )
            exact = (
                same_basis
                and before_count.provenance in EXACT_COUNTS
                and before_count.value is not None
                and after_count.value is not None
            )
            delta = (
                before_count.value - after_count.value
                if same_basis and before_count.value is not None and after_count.value is not None
                else None
            )
            kind = (
                "observed"
                if exact
                else "estimated"
                if same_basis and before_count.provenance == "estimated_words"
                else "declared"
            )
            receipt.update(
                state="applied",
                output_input_hash=fingerprint(after),
                token_counts={"before": before_count.to_dict(), "after": after_count.to_dict()},
                compression_usage=[usage.to_dict() for usage in usages],
            )
            record_experiment_observations(
                {
                    "context_applied": observation(True, unit="boolean", source_ref=source),
                    "context_state": observation("applied", unit="state", source_ref=source),
                    "input_tokens_before": observation(
                        before_count.value,
                        kind=_count_kind(before_count),
                        unit="tokens",
                        source_ref=source,
                    ),
                    "input_tokens_after": observation(
                        after_count.value,
                        kind=_count_kind(after_count),
                        unit="tokens",
                        source_ref=source,
                    ),
                    "input_tokens_removed": observation(
                        delta, kind=kind, unit="tokens", source_ref=source
                    ),
                    "exact_token_reduction": observation(
                        delta > 0 if exact else None, unit="boolean", source_ref=source
                    ),
                }
            )
            _deadline(request)
            result = backend.invoke(replace(request, _input_json=canonical(after)))
            if type(result) is not ExperimentResult:
                _close_invalid(result)
                raise ValueError("backend must return ExperimentResult")
            receipt["backend_usage"] = result.usage.to_dict()
            usages.append(result.usage)
            return ExperimentResult(result.output, usage=_sum_usage(usages))
        except BaseException:
            if receipt["state"] == "started":
                receipt["state"] = "failed"
                try:
                    record_experiment_observations(
                        {
                            "context_applied": observation(
                                False, unit="boolean", source_ref=source
                            ),
                            "context_state": observation("failed", unit="state", source_ref=source),
                        }
                    )
                except BaseException:
                    receipt["capture_error"] = "observation_capture_failed"
            raise

    return ExperimentRunner(
        candidate_id,
        "1.0",
        "agentloop.context-reduction:1.0",
        "sha256:" + config_hash,
        invoke,
        reservation=reservation
        if reservation is not None
        else Reservation()
        if compressing
        else backend.reservation,
    )
