"""Frozen decision-step measurements, separate from model-only profile totals."""

from __future__ import annotations

import json
from dataclasses import asdict

from agentloop.events import AgentEvent
from agentloop.judgment_types import (
    JudgeIdentity,
    JudgeUsage,
    canonical,
    fingerprint,
    finite,
    is_hash,
    text,
)
from agentloop.substitution_types import SUBSTITUTION_VERSION, TRIAL_KEY
from agentloop.workflow_types import stage_summary, workflow_summary

_STATES = {"completed", "unknown", "failed", "timed_out", "invalid_result", "disabled", "missing"}


def _binding(trace):
    events = []
    for event in trace.events:
        value = AgentEvent.from_dict(event.to_dict()).to_dict()
        value["metadata"] = {
            key: item
            for key, item in value["metadata"].items()
            if key not in {"otel_span_id", "otel_trace_id"}
        }
        events.append(value)
    return fingerprint(
        {
            "run_id": trace.run_id,
            "started_at": trace.started_at,
            "ended_at": trace.ended_at,
            "elapsed_ms": float(trace.elapsed_ms) if trace.elapsed_ms is not None else None,
            "events": events,
            "workflow": trace.metadata.get("agentloop.workflow"),
            "trial_metadata": {
                key: trace.metadata.get(key)
                for key in (
                    "plan_hash",
                    "example_id",
                    "task_id",
                    "repetition",
                    "condition",
                    "synthetic",
                    "success",
                )
            },
        }
    )


def attach_trial_evidence(
    trace,
    *,
    plan_hash,
    example_id,
    repetition,
    implementation,
    status,
    latency_ms,
    usage,
    cost_ref,
    input_hash,
    output_hash,
):
    record = {
        "schema_version": SUBSTITUTION_VERSION,
        "run_id": trace.run_id,
        "plan_hash": plan_hash,
        "example_id": example_id,
        "repetition": repetition,
        "implementation": implementation,
        "outcome": status,
        "latency_ms": latency_ms,
        "usage": asdict(usage),
        "cost_ref": cost_ref,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "source_hash": _binding(trace),
        "scope": "declared_decision_step",
        "timing_basis": "callback_wall_time" if latency_ms is not None else "not_observed",
    }
    record["evidence_hash"] = fingerprint(record)
    if TRIAL_KEY in trace.metadata:
        raise ValueError("trial evidence cannot replace an existing receipt")
    trace.metadata[TRIAL_KEY] = json.loads(canonical(record))
    if read_trial_evidence(trace)["status"] != "valid":
        trace.metadata.pop(TRIAL_KEY)
        raise ValueError("invalid decision trial evidence")
    return json.loads(canonical(record))


def read_trial_evidence(trace):
    if TRIAL_KEY not in trace.metadata:
        return (
            {"status": "invalid", "scope": "declared_decision_step"}
            if trace.metadata.get("source") == "agentloop_substitution"
            else None
        )
    empty = {"status": "invalid", "scope": "declared_decision_step"}
    try:
        record = json.loads(canonical(trace.metadata[TRIAL_KEY]))
        if not isinstance(record, dict):
            return empty
        if record.get("schema_version") != SUBSTITUTION_VERSION:
            return {**empty, "status": "unsupported"}
        digest = record.pop("evidence_hash")
        if (
            digest != fingerprint(record)
            or record["run_id"] != trace.run_id
            or record["source_hash"] != _binding(trace)
        ):
            return empty
        if record["scope"] != "declared_decision_step" or record["outcome"] not in _STATES:
            return empty
        for key in ("plan_hash", "input_hash"):
            if not is_hash(record[key]):
                return empty
        if record["output_hash"] is not None and not is_hash(record["output_hash"]):
            return empty
        if (record["outcome"] == "completed") != (record["output_hash"] is not None):
            return empty
        text(record["example_id"], "example_id")
        if type(record["repetition"]) is not int or record["repetition"] < 0:
            return empty
        declaration = record["implementation"]
        text(declaration["name"], "implementation name")
        JudgeIdentity(**declaration["identity"])
        if declaration["kind"] not in {"model", "classifier", "rule", "tool", "external_service"}:
            return empty
        JudgeUsage(**record["usage"])
        for key in ("plan_hash", "example_id", "repetition"):
            if trace.metadata.get(key) != record[key]:
                return empty
        if (
            trace.metadata.get("condition") != declaration["name"]
            or type(trace.metadata.get("synthetic")) is not bool
        ):
            return empty
        if trace.metadata.get("task_id") != record["example_id"]:
            return empty
        workflow = workflow_summary(trace.metadata)
        if workflow is None or workflow.get("schema_status") != "supported":
            return empty
        failed = record["outcome"] in {"failed", "timed_out", "invalid_result"}
        expected_state = (
            "failed"
            if failed
            else ("unknown" if record["outcome"] in {"disabled", "missing"} else "completed")
        )
        if workflow["status"] != expected_state:
            return empty
        text(record["cost_ref"], "cost_ref", optional=True)
        latency = record["latency_ms"]
        if latency is None:
            if (
                record["outcome"] not in {"disabled", "missing"}
                or record["timing_basis"] != "not_observed"
                or trace.events
            ):
                return empty
        elif (
            not finite(latency)
            or latency < 0
            or latency != trace.elapsed_ms
            or record["timing_basis"] != "callback_wall_time"
            or len(trace.events) != 1
            or trace.events[0].duration_ms != latency
        ):
            return empty
        if latency is not None and trace.events[0].status != ("error" if failed else "ok"):
            return empty
        if latency is not None:
            stage = stage_summary(trace.events[0])
            if (
                stage is None
                or stage.get("schema_status") != "supported"
                or stage.get("version") != declaration["identity"]["version"]
                or stage.get("kind") != declaration["kind"]
                or stage.get("outcome") != record["outcome"]
                or stage.get("output_ref")
                != (
                    "sha256:" + record["output_hash"] if record["output_hash"] is not None else None
                )
            ):
                return empty
        if record["outcome"] == "disabled" and JudgeUsage(**record["usage"]) != JudgeUsage(
            0, 0, 0, "not_dispatched", "not_dispatched"
        ):
            return empty
        return {**record, "status": "valid", "evidence_hash": digest}
    except (TypeError, ValueError, KeyError, AttributeError):
        return empty
