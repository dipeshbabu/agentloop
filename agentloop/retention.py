"""Loss-aware native trace retention. No callbacks run except an explicit redactor."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import random
from collections import Counter
from threading import Lock
from typing import Any, Callable

from agentloop.events import AgentEvent
from agentloop.retention_types import (
    RETENTION_KEY,
    RETENTION_VERSION,
    RetentionContext,
    RetentionPolicy,
)
from agentloop.workflow_types import stage_summary, workflow_summary


def _digest(value: Any) -> str:
    # Streaming encoding avoids a second full serialized trace in memory.
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), allow_nan=False
    ).iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _summary(value: Any) -> dict:
    return {
        "sha256": _digest(value),
        "type": type(value).__name__,
        "length": len(value) if isinstance(value, (str, list, dict)) else None,
    }


def _alias(value: str) -> str:
    return "sha256:" + _digest(value)


def _binding(trace) -> dict:
    value = trace.to_dict()
    value["metadata"] = dict(value["metadata"])
    value["metadata"].pop(RETENTION_KEY, None)
    return value


def read_retention(trace) -> dict | None:
    """Validate content binding; hashes detect mutation, not hostile forgery."""
    metadata = getattr(trace, "metadata", {})
    if not isinstance(metadata, dict) or RETENTION_KEY not in metadata:
        return None
    envelope = metadata[RETENTION_KEY]
    if not isinstance(envelope, dict) or envelope.get("schema_version") != RETENTION_VERSION:
        raise ValueError("unsupported retention evidence")
    content = dict(envelope)
    checksum = content.pop("sha256", None)
    if checksum != _digest(content) or content.get("artifact_sha256") != _digest(_binding(trace)):
        raise ValueError("retention evidence no longer matches trace")
    if (
        not isinstance(content.get("metrics"), dict)
        or type(content.get("complete_evidence")) is not bool
    ):
        raise ValueError("invalid retention evidence")
    return copy.deepcopy(envelope)


def require_complete_evidence(trace, analysis: str) -> None:
    evidence = read_retention(trace)
    if evidence is not None and not evidence["complete_evidence"]:
        raise ValueError(
            f"{analysis} requires complete trace evidence; retention omitted required evidence"
        )


def retained_report(trace) -> dict | None:
    evidence = read_retention(trace)
    if evidence is None or evidence["complete_evidence"]:
        return None
    report = copy.deepcopy(evidence["metrics"])
    report.update(
        run_id=trace.run_id,
        name=trace.name,
        events=[event.to_dict() for event in trace.events],
        retention=evidence,
        retained_event_count=len(trace.events),
        finding_candidates=[],
        parallelism_opportunities=[],
        analysis_complete=False,
        rule_errors=[
            {
                "rule_id": "retention.incomplete_evidence",
                "message": "Only recorded aggregate metrics are available; missing evidence prevents finding analysis.",
            }
        ],
        recommendations=[
            {
                "title": "Analysis unavailable after retention",
                "description": "Use the original trace for findings, replay, quality or study comparisons.",
            }
        ],
    )
    return report


def _metrics(trace) -> dict:
    from agentloop.metrics import build_report

    report = build_report(trace, _aggregate_only=True)
    for key in ("events", "recommendations", "parallelism_opportunities", "name", "run_id"):
        report.pop(key, None)
    cost = report["cost_breakdown"]
    cost.pop("model_calls", None)
    cost["unknown_models"] = [_alias(model) for model in cost["unknown_models"]]
    cost["detail_availability"] = "aggregate_only"
    report["error_span_count"] = sum(event.status == "error" for event in trace.events)
    return report


class RetentionSession:
    """Bounded stream state; calls are serialized and counts describe calls, not unique runs.

    The session does not persist raw traces or deduplicate run IDs. A host must
    supply each completed execution once. Repeated keys form a cluster sample.
    """

    def __init__(self, policy: RetentionPolicy, *, redactor: Callable[[str], str] | None = None):
        if not isinstance(policy, RetentionPolicy):
            raise TypeError("policy must be RetentionPolicy")
        if (policy.payloads == "redact" and not callable(redactor)) or (
            policy.payloads != "redact" and redactor is not None
        ):
            raise ValueError(
                "redact mode requires an explicit trusted redactor, and only that mode accepts one"
            )
        self._policy = policy
        self._redactor = redactor
        self._rng = random.Random(policy.seed)  # nosec B311: reproducible telemetry sampling
        self._lock = Lock()
        self._seen = self._retained = 0
        self._buckets: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    def summary(self) -> dict:
        with self._lock:
            return self._summary()

    def _summary(self) -> dict:
        return {
            "schema_version": RETENTION_VERSION,
            "policy": self.policy.to_dict(),
            "observed_input_count": self._seen,
            "observed_sample_count": self._retained,
            "estimated_population_count": None,
            "population_estimate_status": "unavailable_without_sampling_frame_and_uncertainty_model",
            "effective_rate": self._retained / self._seen if self._seen else None,
            "rate_scope": "session_prefix_calls",
            "retention_reason_counts": dict(self._reasons),
            "tracked_bucket_count": len(self._buckets),
        }

    def retain(self, trace, *, context: RetentionContext | None = None):
        """Return an owned native trace or None. Failures never return raw data."""
        from agentloop.schema import coerce_event_dict, validate_trace_dict
        from agentloop.tracer import AgentTrace

        if not isinstance(trace, AgentTrace):
            raise TypeError("trace must be AgentTrace")
        if RETENTION_KEY in trace.metadata:
            raise ValueError("cannot retain an already retained trace")
        if trace.ended_at is None or trace.elapsed_ms is None:
            raise ValueError("retention requires a finished trace")
        context = RetentionContext() if context is None else context
        if not isinstance(context, RetentionContext):
            raise TypeError("context must be RetentionContext")
        with self._lock:
            validate_trace_dict(trace.to_dict())
            for index, event in enumerate(trace.events):
                coerce_event_dict(event.to_dict(), index=index)
            return self._retain(trace, context)

    def _retain(self, trace, context):
        policy = self.policy
        metrics = _metrics(trace)
        reasons: list[str] = []
        execution = workflow_summary(trace.metadata)
        status = execution.get("status") if execution else None
        failure = (
            context.outcome in {"failure", "timeout"}
            or trace.metadata.get("success") is False
            or metrics["error_span_count"] > 0
            or status in {"failed", "cancelled", "interrupted"}
        )
        unknown = (
            (context.outcome == "unknown" and status != "completed")
            or status not in {None, "completed", "failed", "cancelled", "interrupted"}
            or execution is not None
            and execution.get("schema_status") != "supported"
            or metrics["cost_status"] not in {"empty", "complete"}
            or metrics["token_status"] not in {"empty", "exact"}
            or any(event.status not in {"ok", "error"} for event in trace.events)
        )
        for reason, protected in (
            ("failure_or_timeout", policy.retain_failures and failure),
            ("unknown", policy.retain_unknowns and unknown),
            ("disagreement", policy.retain_disagreements and context.disagreement is True),
            ("anomaly", policy.retain_anomalies and context.anomaly is True),
            ("protected_cohort", bool(set(context.cohorts).intersection(policy.protected_cohorts))),
            (
                "high_cost",
                policy.high_cost_usd is not None
                and metrics["estimated_cost_usd"] >= policy.high_cost_usd,
            ),
            (
                "high_latency",
                policy.high_latency_ms is not None
                and metrics["total_runtime_ms"] >= policy.high_latency_ms,
            ),
        ):
            if protected:
                reasons.append(reason)
        buckets: set[str] = set()
        if policy.representatives_per_bucket:
            new_buckets = 0
            for event in trace.events:
                stage = stage_summary(event) or {}
                bucket = _digest(
                    [
                        stage.get("stage_id") or event.name,
                        stage.get("outcome") or context.outcome,
                        event.status,
                    ]
                )
                if bucket in buckets:
                    continue
                if bucket in self._buckets or len(self._buckets) + new_buckets < policy.max_buckets:
                    buckets.add(bucket)
                    new_buckets += bucket not in self._buckets
            if any(self._buckets[bucket] < policy.representatives_per_bucket for bucket in buckets):
                reasons.append("representative_bucket")
        key = context.sample_key or trace.run_id
        draw = (
            int(_digest([policy.seed, key]), 16) / 2**256
            if policy.sampling == "deterministic"
            else self._rng.random()
        )
        if draw < policy.sample_rate:
            reasons.append("sample")
        if not reasons:
            self._seen += 1
            return None
        source_hash = _digest(trace.to_dict())
        derived, indices = self._compact(trace)
        if self._redactor is not None and _digest(trace.to_dict()) != source_hash:
            raise ValueError("source trace changed during redaction")
        envelope = {
            "schema_version": RETENTION_VERSION,
            "policy": policy.to_dict(),
            "policy_sha256": _digest(policy.to_dict()),
            "source_sha256": source_hash,
            "source_metadata": _summary(trace.metadata),
            "original_event_count": len(trace.events),
            "retained_event_count": len(derived.events),
            "original_event_order_sha256": _digest([event.event_id for event in trace.events]),
            "retained_original_indices": indices,
            "complete_evidence": policy.mode == "full" and policy.payloads == "capture",
            "execution_outcome": "failure"
            if failure
            else "success"
            if status == "completed"
            or execution is None
            and (context.outcome == "success" or trace.metadata.get("success") is True)
            else "unknown",
            "metrics": metrics,
            "reasons": reasons,
            "base_sample_rate": policy.sample_rate,
            "conditional_inclusion_rate": 1.0
            if any(reason != "sample" for reason in reasons)
            else policy.sample_rate,
            "sample_key_sha256": _digest(key),
            "context": {
                "outcome": context.outcome,
                "cohort_hashes": [_digest(value) for value in context.cohorts],
                "disagreement": context.disagreement,
                "anomaly": context.anomaly,
            },
            "bucket_hashes": sorted(buckets),
            "artifact_sha256": _digest(_binding(derived)),
        }
        self._seen += 1
        self._retained += 1
        self._reasons.update(reasons)
        for bucket in buckets:
            self._buckets[bucket] += 1
        envelope["sampling"] = self._summary()
        envelope["sha256"] = _digest(envelope)
        derived.metadata[RETENTION_KEY] = envelope
        return derived

    def _compact(self, trace):
        from agentloop.tracer import AgentTrace

        policy = self.policy
        capture = policy.payloads == "capture"
        result = AgentTrace(
            name=trace.name if capture else _alias(trace.name),
            run_id=trace.run_id,
            metadata=copy.deepcopy(trace.metadata) if capture else {},
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            elapsed_ms=trace.elapsed_ms,
        )
        if policy.mode == "metrics_only":
            return result, []
        indices = []
        for index, event in enumerate(trace.events):
            # max_events is soft: errors and unknown statuses survive span compaction.
            protected = (policy.retain_failures and event.status == "error") or (
                policy.retain_unknowns and event.status not in {"ok", "error"}
            )
            if policy.mode == "compact" and index >= policy.max_events and not protected:
                continue
            value = copy.deepcopy(event.to_dict()) if capture else event.to_dict()
            if not capture:
                value["name"] = _alias(event.name)
                value["model"] = _alias(event.model) if event.model is not None else None
                value["metadata"] = {
                    "retention_payloads": {
                        field: _summary(value[field])
                        for field in ("input_text", "output_text", "error", "metadata")
                    }
                }
                stage = stage_summary(event)
                if stage is not None:
                    value["metadata"]["retention_stage"] = {
                        "schema_status": stage["schema_status"],
                        "stage_id_sha256": _digest(stage.get("stage_id")),
                        "version_sha256": _digest(stage.get("version")),
                        "input_schema_ref_sha256": _digest(stage.get("input_schema_ref")),
                        "output_schema_ref_sha256": _digest(stage.get("output_schema_ref")),
                        "outcome_sha256": _digest(stage.get("outcome")),
                    }
                for field in ("input_text", "output_text", "error"):
                    original = value[field]
                    redacted = (
                        self._redactor(original)
                        if self._redactor and original is not None
                        else None
                    )
                    if (
                        self._redactor
                        and original is not None
                        and (not isinstance(redacted, str) or len(redacted) > 100_000)
                    ):
                        if inspect.iscoroutine(redacted):
                            redacted.close()
                        raise ValueError(
                            "redactor must return a string of at most 100000 characters"
                        )
                    value[field] = redacted
            result.add_event(AgentEvent.from_dict(value, index=index))
            indices.append(index)
        return result, indices
