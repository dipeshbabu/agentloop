"""Exact-key reuse from a frozen prior-result snapshot; no cache service."""

from __future__ import annotations

from dataclasses import dataclass, field

from agentloop.ablation_protocol import timestamp
from agentloop.budget_types import ResourceUsage
from agentloop.context_experiments import _path, _value
from agentloop.context_types import identifier
from agentloop.experiment_observations import observation, record_experiment_observations
from agentloop.experiment_types import (
    ExperimentResult,
    ExperimentRunner,
    canonical,
    fingerprint,
    reference,
)
from agentloop.tracer import current_trace

REUSE_KEY = "agentloop.reuse_experiment"


@dataclass(frozen=True, init=False)
class ReuseEntry:
    _json: str = field(repr=False)
    _result: ExperimentResult = field(repr=False)

    def __init__(
        self,
        key,
        output,
        *,
        key_version=None,
        implementation_ref=None,
        implementation_version=None,
        configuration_ref=None,
        data_ref=None,
        invalidation_epoch=None,
        source_ref=None,
        created_at=None,
        expires_at=None,
    ):
        for name, value in (
            ("key_version", key_version),
            ("implementation_ref", implementation_ref),
            ("implementation_version", implementation_version),
            ("configuration_ref", configuration_ref),
            ("data_ref", data_ref),
            ("invalidation_epoch", invalidation_epoch),
            ("source_ref", source_ref),
        ):
            if value is not None:
                reference(value, name)
        for value in (created_at, expires_at):
            if value is not None:
                timestamp(value)
        result = ExperimentResult(output)
        data = {
            "key_hash": fingerprint(key),
            "output_hash": fingerprint(result.output),
            "key_version": key_version,
            "implementation_ref": implementation_ref,
            "implementation_version": implementation_version,
            "configuration_ref": configuration_ref,
            "data_ref": data_ref,
            "invalidation_epoch": invalidation_epoch,
            "source_ref": source_ref,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        object.__setattr__(self, "_json", canonical(data))
        object.__setattr__(self, "_result", result)

    @property
    def output(self):
        return self._result.output

    def declaration(self):
        import json

        return json.loads(self._json)


def result_reuse_runner(
    candidate_id,
    backend,
    *,
    entries,
    key_paths,
    key_version,
    data_ref,
    invalidation_epoch,
    as_of,
    on_unavailable="recompute",
    reservation=None,
):
    if (
        type(backend) is not ExperimentRunner
        or not isinstance(entries, (list, tuple))
        or len(entries) > 1000
        or any(type(entry) is not ReuseEntry for entry in entries)
    ):
        raise ValueError("reuse requires an explicit backend and bounded frozen entry snapshot")
    if not isinstance(key_paths, (list, tuple)) or not 1 <= len(key_paths) <= 32:
        raise ValueError("reuse keys require explicit bounded input paths")
    paths = tuple(_path(path) for path in key_paths)
    if len(set(paths)) != len(paths):
        raise ValueError("reuse key paths must be unique")
    identifier(key_version, "key_version")
    reference(data_ref, "data_ref")
    reference(invalidation_epoch, "invalidation_epoch")
    instant = timestamp(as_of)
    if on_unavailable not in {"recompute", "error"}:
        raise ValueError("unknown reuse fallback policy")
    entries = tuple(entries)
    declarations = [entry.declaration() for entry in entries]
    if sum(len(canonical(entry.output).encode("utf-8")) for entry in entries) > 10_000_000:
        raise ValueError("reuse snapshot exceeds ten million output bytes")
    config = {
        "schema_version": "1.0",
        "backend": backend.declaration(),
        "key_paths": paths,
        "key_version": key_version,
        "data_ref": data_ref,
        "invalidation_epoch": invalidation_epoch,
        "as_of": as_of,
        "on_unavailable": on_unavailable,
        "entries": declarations,
        "snapshot_semantics": "read_only_prior_results_no_implicit_fill",
    }
    config_hash = fingerprint(config)
    source = "reuse-config:" + config_hash
    indexed = {}
    for entry, declared in zip(entries, declarations):
        indexed.setdefault(declared["key_hash"], []).append((entry, declared))
    required = {
        "key_version": key_version,
        "implementation_ref": backend.implementation_ref,
        "implementation_version": backend.version,
        "configuration_ref": backend.configuration_ref,
        "data_ref": data_ref,
        "invalidation_epoch": invalidation_epoch,
    }

    def invoke(request):
        if backend.declaration() != config["backend"]:
            raise ValueError("reuse backend binding changed after configuration")
        trace = current_trace()
        if trace is None:
            raise ValueError("reuse candidate requires an active experiment")
        receipt = {
            "schema_version": "1.0",
            "config_hash": config_hash,
            "key_hash": None,
            "state": "key_unavailable",
            "entry_refs": [],
            "fallback": False,
            "as_of": as_of,
        }
        trace.metadata[REUSE_KEY] = receipt
        valid = []
        try:
            payload = request.inputs
            key = [_value(payload, path) for path in paths]
        except ValueError:
            matches = []
        else:
            receipt["key_hash"] = fingerprint(key)
            matches = indexed.get(receipt["key_hash"], [])
            receipt["state"] = "miss"
        states = []
        for entry, declared in matches:
            receipt["entry_refs"].append(
                {
                    "source_ref": declared["source_ref"],
                    "entry_hash": fingerprint(declared),
                    "output_hash": declared["output_hash"],
                }
            )
            if any(
                declared[key] is None
                for key in (*required, "source_ref", "created_at", "expires_at")
            ):
                states.append("unknown_evidence")
            elif any(declared[key] != value for key, value in required.items()):
                states.append("invalidated")
            elif timestamp(declared["created_at"]) >= timestamp(declared["expires_at"]):
                states.append("invalid_lifetime")
            elif timestamp(declared["created_at"]) > instant:
                states.append("future_entry")
            elif timestamp(declared["expires_at"]) <= instant:
                states.append("stale")
            else:
                valid.append((entry, declared))
        if valid:
            newest = max(timestamp(declared["created_at"]) for _, declared in valid)
            selected = [
                (entry, declared)
                for entry, declared in valid
                if timestamp(declared["created_at"]) == newest
            ]
            if len({declared["output_hash"] for _, declared in selected}) == 1:
                chosen = min(selected, key=lambda item: fingerprint(item[1]))
                receipt.update(state="hit", selected_entry_hash=fingerprint(chosen[1]))
            else:
                receipt["state"] = "ambiguous"
        elif states:
            receipt["state"] = sorted(states)[0]
        receipt["unusable_entry_states"] = sorted(states)
        hit = receipt["state"] == "hit"
        expired = not hit and on_unavailable == "recompute" and request.remaining_s <= 0
        receipt["fallback"] = not hit and on_unavailable == "recompute" and not expired
        record_experiment_observations(
            {
                "cache_hit": observation(hit, unit="boolean", source_ref=source),
                "cache_state": observation(receipt["state"], unit="state", source_ref=source),
                "cache_fallback_invoked": observation(
                    receipt["fallback"], unit="boolean", source_ref=source
                ),
                "cache_fill_cost_included": observation(
                    False, kind="declared", unit="boolean", source_ref=source
                ),
            }
        )
        if hit:
            # No provider token work is performed by a snapshot hit. Lookup and
            # prior fill operating costs remain unknown/excluded, not free.
            # token_provenance is an accounting enum, not a credential.
            return ExperimentResult(
                chosen[0].output,
                usage=ResourceUsage(tokens=0, token_provenance="user_supplied", complete=True),  # nosec B106
            )
        if expired:
            raise TimeoutError("reuse fallback deadline expired")
        if on_unavailable == "error":
            raise ValueError("reuse evidence is unavailable under the declared key contract")
        return backend.invoke(request)

    return ExperimentRunner(
        candidate_id,
        "1.0",
        "agentloop.result-reuse:1.0",
        "sha256:" + config_hash,
        invoke,
        reservation=reservation if reservation is not None else backend.reservation,
    )
