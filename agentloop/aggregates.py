"""Streaming native-trace aggregates with explicit completeness and merge semantics."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from agentloop.aggregate_stats import AggregateConfig, Histogram, WeightedCandidates, number, total
from agentloop.costs import load_pricing_table
from agentloop.metrics import _event_cost_estimate
from agentloop.operations import OPERATION_KINDS, operation_kind
from agentloop.retention import RetentionPolicy, read_retention
from agentloop.schema import coerce_event_dict, validate_trace_dict
from agentloop.timing import elapsed_runtime_ms
from agentloop.tokens import EXACT_PROVENANCE, WRITABLE_PROVENANCE, provenance_grade
from agentloop.tracer import AgentTrace
from agentloop.workflow_types import stage_summary, workflow_summary

AGGREGATE_VERSION = "1.0"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_COUNTERS = (
    "runs",
    "events",
    "model_calls",
    "tool_calls",
    "retries",
    "input_tokens",
    "output_tokens",
    "error_spans",
    "stage_spans",
    "decision_spans",
    "stage_evidence_missing_runs",
    "inexact_token_runs",
    "unknown_cost_runs",
    "invalid_inputs",
    "undeclared_stage_spans",
    "cost_detail_missing_runs",
)
_CATEGORIES = {
    "outcomes": {"success", "failure", "unknown"},
    "operations": OPERATION_KINDS | {"unknown"},
    "token_provenance": WRITABLE_PROVENANCE | {"unspecified"},
    "cost_calls": {"calculated", "provider_reported", "unknown", "priced_unsplit"},
    "runtime_basis": {"explicit_elapsed", "legacy_fallback", "retained_snapshot"},
}
_SKETCHES = (
    "stage_counts",
    "stage_latency_ms",
    "stage_known_cost_usd",
    "decision_outcomes",
    "cost_provenance",
)


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _increment(counter, key, value=1):
    if type(value) is not int or value < 0:
        raise ValueError("aggregate counts must be nonnegative integers")
    counter[key] += value


class TraceAggregate:
    """Bounded summary state. Input iteration loads one trace at a time.

    Counts refer to supplied records, not unique run IDs. Inputs and partitions
    must be disjoint; no unbounded deduplication registry is maintained.
    """

    def __init__(self, config: AggregateConfig | None = None, *, pricing=None):
        self.config = AggregateConfig() if config is None else config
        if not isinstance(self.config, AggregateConfig):
            raise TypeError("config must be AggregateConfig")
        self._pricing = copy.deepcopy(pricing if pricing is not None else load_pricing_table())
        self._pricing_hash = _hash(asdict(self._pricing))
        self._reset()

    def _reset(self):
        self._semantics = None
        self._counts = Counter({key: 0 for key in _COUNTERS})
        self._categories = {
            key: Counter({value: 0 for value in sorted(values)})
            for key, values in _CATEGORIES.items()
        }
        self._latency = Histogram(self.config.latency_bounds_ms)
        self._cost = {"calculated_usd": 0.0, "provider_reported_usd": 0.0}
        self._sketches = {
            key: WeightedCandidates(self.config.heavy_hitter_capacity) for key in _SKETCHES
        }

    def add(self, trace: AgentTrace) -> None:
        if not isinstance(trace, AgentTrace):
            raise TypeError("aggregate input must be AgentTrace")
        # Build an isolated bounded delta, so any malformed metric leaves state intact.
        delta = copy.copy(self)
        delta._reset()
        delta._consume(trace)
        self.merge(delta)

    def consume(self, traces, *, on_error="raise"):
        if on_error not in {"raise", "skip"}:
            raise ValueError("on_error must be raise or skip")
        for trace in traces:
            try:
                self.add(trace)
            except (ValueError, TypeError, OverflowError):
                if on_error == "raise":
                    raise
                self._counts["invalid_inputs"] += 1
        return self

    def _consume(self, trace):
        validate_trace_dict(trace.to_dict())
        if getattr(trace, "_timing_active", False):
            raise ValueError("aggregate input trace is still running")
        for index, event in enumerate(trace.events):
            coerce_event_dict(event.to_dict(), index=index)
        retained = read_retention(trace)
        self._semantics = {
            "retention_policy": retained["policy"] if retained else None,
            "cost_basis": "retained_metric_snapshots" if retained else "offline_pricing_table",
            "pricing_sha256": None if retained else self._pricing_hash,
        }
        self._counts["runs"] = 1
        if retained:
            metrics = retained["metrics"]
            for target, source in (
                ("events", "event_count"),
                ("model_calls", "model_call_count"),
                ("tool_calls", "tool_call_count"),
                ("retries", "retry_count"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("error_spans", "error_span_count"),
            ):
                _increment(self._counts, target, metrics[source])
            self._latency.add(metrics["total_runtime_ms"])
            self._categories["runtime_basis"]["retained_snapshot"] = 1
            for kind, count in metrics["operation_counts"].items():
                _increment(
                    self._categories["operations"],
                    kind if kind in OPERATION_KINDS else "unknown",
                    count,
                )
            for kind, count in metrics["token_provenance_counts"].items():
                _increment(
                    self._categories["token_provenance"],
                    kind if kind in WRITABLE_PROVENANCE else "unspecified",
                    count,
                )
            self._counts["inexact_token_runs"] = int(
                metrics["token_status"] not in {"exact", "empty"}
            )
            self._counts["unknown_cost_runs"] = int(
                metrics["cost_status"] not in {"complete", "empty"}
            )
            costs = metrics["cost_breakdown"]
            for key in self._cost:
                self._cost[key] = number(costs[key])
            # Older retention snapshots do not split priced call counts by source.
            self._categories["cost_calls"]["unknown"] = costs["unavailable_model_call_count"]
            self._categories["cost_calls"]["priced_unsplit"] = costs["priced_model_call_count"]
            self._counts["cost_detail_missing_runs"] = int(metrics["model_call_count"] > 0)
            self._sketches["cost_provenance"].add(
                _hash([costs["pricing_sources"], costs["pricing_as_of"]]), 1
            )
            outcome = retained.get("execution_outcome", retained["context"]["outcome"])
            failure = (
                metrics["error_span_count"] > 0
                or "failure_or_timeout" in retained["reasons"]
                or outcome in {"failure", "timeout"}
            )
            self._categories["outcomes"][
                "failure" if failure else "success" if outcome == "success" else "unknown"
            ] = 1
            if not retained["complete_evidence"]:
                self._counts["stage_evidence_missing_runs"] = 1
                return
            # Original per-event prices may differ; never reprice retained snapshots.
            for event in trace.events:
                self._stage(event, cost=None)
            return
        self._latency.add(round(elapsed_runtime_ms(trace), 3))
        self._categories["runtime_basis"][
            "explicit_elapsed" if trace.elapsed_ms is not None else "legacy_fallback"
        ] = 1
        failed = False
        for event in trace.events:
            self._counts["events"] += 1
            kind = operation_kind(event)
            self._categories["operations"][kind] += 1
            self._counts["tool_calls"] += event.event_type == "tool_call"
            self._counts["retries"] += event.event_type == "retry"
            failed |= event.status == "error"
            self._counts["error_spans"] += event.status == "error"
            cost = None
            if event.event_type == "model_call":
                self._counts["model_calls"] += 1
                self._counts["input_tokens"] += event.input_tokens
                self._counts["output_tokens"] += event.output_tokens
                provenance = (
                    event.token_provenance
                    if event.token_provenance in WRITABLE_PROVENANCE
                    else "unspecified"
                )
                self._categories["token_provenance"][provenance] += 1
                self._counts["inexact_token_runs"] |= provenance not in EXACT_PROVENANCE
                estimate = _event_cost_estimate(event, self._pricing)
                self._categories["cost_calls"][estimate.state] += 1
                if estimate.amount_usd is None:
                    self._counts["unknown_cost_runs"] = 1
                else:
                    cost = estimate.amount_usd
                    self._cost[estimate.state + "_usd"] = total(
                        self._cost[estimate.state + "_usd"], cost
                    )
                self._sketches["cost_provenance"].add(
                    _hash(
                        [
                            estimate.pricing_source,
                            estimate.pricing_as_of,
                            provenance_grade(event.token_provenance),
                        ]
                    ),
                    1,
                )
            self._stage(event, cost=cost)
        execution = workflow_summary(trace.metadata)
        status = (
            execution.get("status")
            if execution and execution.get("schema_status") == "supported"
            else None
        )
        failed |= (
            status in {"failed", "cancelled", "interrupted"}
            or trace.metadata.get("success") is False
        )
        succeeded = (
            status == "completed"
            if execution is not None
            else trace.metadata.get("success") is True
        )
        self._categories["outcomes"][
            "failure" if failed else "success" if succeeded else "unknown"
        ] = 1

    def _stage(self, event, *, cost):
        stage = stage_summary(event)
        if stage is None or stage.get("schema_status") != "supported" or not stage.get("stage_id"):
            self._counts["undeclared_stage_spans"] += 1
            return
        key = _hash([stage["stage_id"], stage.get("version"), stage["kind"]])
        self._counts["stage_spans"] += 1
        self._sketches["stage_counts"].add(key, 1)
        self._sketches["stage_latency_ms"].add(key, event.duration_ms)
        if cost is not None:
            self._sketches["stage_known_cost_usd"].add(key, cost)
        if stage["kind"] in {"classifier", "rule"}:
            self._counts["decision_spans"] += 1
            self._sketches["decision_outcomes"].add(
                _hash(
                    [stage["stage_id"], stage.get("version"), stage.get("outcome"), event.status]
                ),
                1,
            )

    def merge(self, other):
        if not isinstance(other, TraceAggregate) or self.config != other.config:
            raise ValueError("incompatible aggregate configuration")
        if (
            self._semantics is not None
            and other._semantics is not None
            and self._semantics != other._semantics
        ):
            raise ValueError("incompatible retention, sampling or pricing semantics")
        owned = copy.copy(self)
        owned._counts = self._counts.copy()
        owned._categories = {key: value.copy() for key, value in self._categories.items()}
        owned._latency = copy.deepcopy(self._latency)
        owned._cost = dict(self._cost)
        owned._sketches = copy.deepcopy(self._sketches)
        owned._semantics = copy.deepcopy(
            self._semantics if self._semantics is not None else other._semantics
        )
        owned._counts.update(other._counts)
        for key in owned._categories:
            owned._categories[key].update(other._categories[key])
        owned._latency.merge(other._latency)
        for key in owned._cost:
            owned._cost[key] = total(owned._cost[key], other._cost[key])
        for key in owned._sketches:
            owned._sketches[key].merge(other._sketches[key])
        self.__dict__.update(owned.__dict__)
        return self

    def to_dict(self):
        counts = dict(self._counts)
        snapshot_costs = counts["cost_detail_missing_runs"]
        value = {
            "schema_version": AGGREGATE_VERSION,
            "kind": "agentloop.trace_aggregate",
            "config": self.config.to_dict(),
            "semantics": copy.deepcopy(self._semantics),
            "counts": counts,
            "categories": {key: dict(value) for key, value in self._categories.items()},
            "latency": self._latency.to_dict(),
            "cost": dict(self._cost),
            "heavy_hitters": {key: value.to_dict() for key, value in self._sketches.items()},
            "completeness": {
                "input_tokens": None if counts["inexact_token_runs"] else counts["input_tokens"],
                "output_tokens": None if counts["inexact_token_runs"] else counts["output_tokens"],
                "known_model_cost_usd": total(*self._cost.values()),
                "pricing_complete_model_cost_usd": None
                if counts["unknown_cost_runs"]
                else total(*self._cost.values()),
                "priced_call_source_split_missing_runs": snapshot_costs,
                "stage_cost_missing_runs": snapshot_costs,
                "stage_metrics_scope": "complete-evidence runs with declared stages only",
                "cost_scope": "recorded model calls; calculated and provider-reported kept separate",
                "token_scope": "recorded model calls; reported totals can include estimates",  # nosec B105: metric scope, not a credential
            },
            "sampling": {
                "observed_record_count": counts["runs"],
                "input_population_count": None,
                "estimated_population_count": None,
                "effective_population_rate": None,
                "basis": "supplied records only; session prefix denominators are not additive",
            },
            "analysis": {
                "findings_available": False,
                "reason": "Individual evidence spans are required for findings, replay and quality analysis.",
            },
        }
        value["sha256"] = _hash(value)
        return value

    @classmethod
    def from_dict(cls, value):
        """Read our versioned aggregate, checking its bound state and derived fields."""
        try:
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != AGGREGATE_VERSION
                or value.get("kind") != "agentloop.trace_aggregate"
            ):
                raise ValueError("unsupported aggregate schema")
            content = dict(value)
            if content.pop("sha256", None) != _hash(content):
                raise ValueError("aggregate checksum mismatch")
            config = value["config"]
            result = cls(
                AggregateConfig(tuple(config["latency_bounds_ms"]), config["heavy_hitter_capacity"])
            )
            semantics = value["semantics"]
            if semantics is not None:
                if not isinstance(semantics, dict) or set(semantics) != {
                    "retention_policy",
                    "cost_basis",
                    "pricing_sha256",
                }:
                    raise ValueError("invalid aggregate semantics")
                retention = semantics["retention_policy"]
                if retention is not None:
                    options = dict(retention)
                    if options.pop("schema_version") != "1.0":
                        raise ValueError("unsupported retention policy")
                    options["protected_cohorts"] = tuple(options["protected_cohorts"])
                    if (
                        RetentionPolicy(**options).to_dict() != retention
                        or semantics["cost_basis"] != "retained_metric_snapshots"
                        or semantics["pricing_sha256"] is not None
                    ):
                        raise ValueError("invalid retained aggregate semantics")
                elif semantics["cost_basis"] != "offline_pricing_table" or not _sha(
                    semantics["pricing_sha256"]
                ):
                    raise ValueError("invalid pricing semantics")
            result._semantics = copy.deepcopy(semantics)
            if set(value["counts"]) != set(_COUNTERS):
                raise ValueError("invalid aggregate counters")
            for key, count in value["counts"].items():
                _increment(result._counts, key, count)
            if set(value["categories"]) != set(_CATEGORIES):
                raise ValueError("invalid aggregate categories")
            for category, choices in _CATEGORIES.items():
                source = value["categories"][category]
                if set(source) != choices:
                    raise ValueError("invalid aggregate category keys")
                for key, count in source.items():
                    _increment(result._categories[category], key, count)
            histogram = value["latency"]
            counts = histogram["counts"]
            if len(counts) != len(result._latency.counts) or any(
                type(count) is not int or count < 0 for count in counts
            ):
                raise ValueError("invalid histogram counts")
            result._latency.counts = list(counts)
            result._latency.count = sum(counts)
            result._latency.sum = number(histogram["sum_ms"])
            for attr, key in (("minimum", "min_ms"), ("maximum", "max_ms")):
                setattr(result._latency, attr, number(histogram[key]) if sum(counts) else None)
            for key in result._cost:
                result._cost[key] = number(value["cost"][key])
            for key, sketch in result._sketches.items():
                source = value["heavy_hitters"][key]
                if len(source["candidates"]) > sketch.capacity:
                    raise ValueError("aggregate candidate capacity exceeded")
                sketch.total = number(source["total_weight"])
                sketch.error = number(source["unlisted_key_upper_bound"])
                for item in source["candidates"]:
                    label = item["key_sha256"]
                    weight = number(item["lower_bound"])
                    if (
                        not _sha(label)
                        or label in sketch.weights
                        or weight <= 0
                        or weight > sketch.total
                    ):
                        raise ValueError("invalid aggregate candidate")
                    sketch.weights[label] = weight
            runs = result._counts["runs"]
            if (
                runs != sum(result._categories["outcomes"].values())
                or runs != result._latency.count
                or runs != sum(result._categories["runtime_basis"].values())
            ):
                raise ValueError("inconsistent aggregate run counts")
            if bool(runs) != (semantics is not None):
                raise ValueError("aggregate semantics missing or unexpected")
            counters = result._counts
            if (
                counters["model_calls"] + counters["tool_calls"] + counters["retries"]
                > counters["events"]
                or counters["error_spans"] > counters["events"]
                or counters["stage_spans"] + counters["undeclared_stage_spans"] > counters["events"]
                or counters["decision_spans"] > counters["stage_spans"]
                or not runs
                and any(count for key, count in counters.items() if key != "invalid_inputs")
                or not counters["model_calls"]
                and (
                    counters["input_tokens"]
                    or counters["output_tokens"]
                    or any(result._cost.values())
                )
                or result._sketches["stage_counts"].total != counters["stage_spans"]
                or result._sketches["decision_outcomes"].total != counters["decision_spans"]
            ):
                raise ValueError("inconsistent aggregate metric counts")
            if (
                sum(result._categories["operations"].values()) != result._counts["events"]
                or sum(result._categories["cost_calls"].values()) != result._counts["model_calls"]
                or sum(result._categories["token_provenance"].values())
                != result._counts["model_calls"]
            ):
                raise ValueError("inconsistent aggregate event counts")
            for key in (
                "inexact_token_runs",
                "unknown_cost_runs",
                "stage_evidence_missing_runs",
                "cost_detail_missing_runs",
            ):
                if result._counts[key] > runs:
                    raise ValueError("aggregate completeness count exceeds runs")
            if runs:
                minimum, maximum = result._latency.minimum, result._latency.maximum
                mean = result._latency.sum / runs
                if (
                    minimum > maximum
                    or mean < minimum
                    and not math.isclose(mean, minimum, rel_tol=1e-12)
                    or mean > maximum
                    and not math.isclose(mean, maximum, rel_tol=1e-12)
                ):
                    raise ValueError("inconsistent aggregate latency bounds")
            elif result._latency.sum:
                raise ValueError("nonzero latency in empty aggregate")
            for sketch in result._sketches.values():
                if (
                    sketch.error > sketch.total
                    or sum(sketch.weights.values()) > sketch.total
                    and not math.isclose(sum(sketch.weights.values()), sketch.total, rel_tol=1e-12)
                ):
                    raise ValueError("inconsistent aggregate candidate bounds")
            if result.to_dict() != value:
                raise ValueError("inconsistent aggregate derived fields")
            return result
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("invalid aggregate artifact") from exc


def _sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def read_json(path, *, max_bytes=MAX_ARTIFACT_BYTES):
    """Bounded per-file read, including files whose size changes while reading."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_bytes must be 1..1073741824")
    with Path(path).open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("aggregate input exceeds byte limit")
    return json.loads(payload)


def aggregate_files(paths, *, config=None, on_error="raise", max_bytes=MAX_ARTIFACT_BYTES):
    result = TraceAggregate(config)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_bytes must be 1..1073741824")
    if on_error not in {"raise", "skip"}:
        raise ValueError("on_error must be raise or skip")
    for path in paths:
        try:
            result.add(AgentTrace.from_dict(read_json(path, max_bytes=max_bytes)))
        except (OSError, ValueError, TypeError, OverflowError):
            if on_error == "raise":
                raise
            result._counts["invalid_inputs"] += 1
    return result
