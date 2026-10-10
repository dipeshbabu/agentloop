"""Omnigent source aliases, session correlation and qualified policy evidence."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentloop.integrations.omnigent.evidence import attach_observations
from agentloop.interoperability.artifacts import native_bytes, write_artifact, write_import_bundle
from agentloop.interoperability.contracts import ImportReceipt, external_id
from agentloop.interoperability.evidence import EXTERNAL_KEY
from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp
from agentloop.interoperability.validation import (
    ImportLimits,
    ImportValidationError,
    load_json_artifact,
)
from agentloop.interventions import canonical_json
from agentloop.tracer import AgentTrace

CONTRACT_REVISION = "a2956be0e97bb175a60b274053d836f07d494c6c"
_ALIASES = {
    "agent_name": ("gen_ai.agent.name", "agent.name"),
    "agent_id": ("omnigent.agent.id", "agent.id"),
    "parent_agent_id": ("omnigent.parent_agent.id", "parent.agent.id"),
    "parent_agent_name": ("parent.agent.name",),
    "session_id": ("session.id", "gen_ai.conversation.id"),
    "parent_session_id": ("omnigent.parent_session.id", "parent.session.id"),
    "request_id": ("omnigent.request.id", "request.id"),
    "turn_id": ("omnigent.turn.id", "turn.id"),
    "harness": ("omnigent.harness", "harness.name", "harness.type", "runtime.harness"),
    "harness_version": ("omnigent.harness.version", "harness.version", "runtime.version"),
    "transport": ("omnigent.integration_mode",),
    "skill_name": ("omnigent.skill.name",),
}


def _label(value: Any) -> str | None:
    return (
        value if isinstance(value, str) and value.strip() and len(value.encode()) <= 512 else None
    )


def _attributes(event: Any) -> dict:
    return {**event.metadata.get("otel_resource_attributes", {}), **event.metadata}


def _aliases(event: Any) -> dict:
    attrs = _attributes(event)
    result = {}
    for key, names in _ALIASES.items():
        labels = {value for name in names if (value := _label(attrs.get(name))) is not None}
        result[key] = next(iter(labels)) if len(labels) == 1 else None
    return result


def _timings(events: list[Any], *, conflicting: bool = False) -> dict:
    result = {}
    for role in ("agent", "model", "tool", "guardrail", "approval", "host", "unknown"):
        selected = [event for event in events if _role(event) == role]
        intervals = sorted(
            (
                int(event.metadata["source_interval"]["start_unix_nano"]),
                int(event.metadata["source_interval"]["end_unix_nano"]),
            )
            for event in selected
            if event.metadata.get("timing_available") is True
        )
        union = 0
        end = None
        for a, b in intervals:
            union += b - a if end is None or a >= end else max(0, b - end)
            end = b if end is None else max(end, b)
        complete = bool(selected) and len(intervals) == len(selected) and not conflicting
        result[role] = {
            "spans": len(selected),
            "timed_spans": len(intervals),
            "interval_union_ms": union / 1_000_000 if complete else None,
            "known_interval_union_ms": union / 1_000_000 if intervals else None,
            "span_cumulative_ms": sum(b - a for a, b in intervals) / 1_000_000
            if complete
            else None,
            "coverage": "complete_for_recorded_spans"
            if complete
            else "partial"
            if intervals
            else "unknown",
        }
    return result


def _role(event: Any) -> str:
    attrs = _attributes(event)
    declared = attrs.get("omnigent.span.role")
    if isinstance(declared, str) and declared in {"approval", "host"}:
        return declared
    return (
        event.operation_kind
        if event.operation_kind in {"agent", "model", "tool", "guardrail"}
        else "unknown"
    )


def _policy(event: Any, tools: list[Any]) -> dict:
    attrs = _attributes(event)
    action = _label(attrs.get("policy.action"))
    canonical = (
        action.upper()
        if action is not None and action.upper() in {"ALLOW", "DENY", "ASK"}
        else "UNKNOWN"
    )
    call_id = _label(attrs.get("policy.tool_call_id")) or _label(attrs.get("tool_call_id"))
    matched = [
        tool
        for tool in tools
        if call_id is not None and _label(_attributes(tool).get("tool_call_id")) == call_id
    ]
    phase = _label(attrs.get("policy.phase"))
    return {
        "event_id": event.event_id,
        "source_span_id": event.metadata.get("source_span_id"),
        "name": _label(attrs.get("policy.name")),
        "phase": phase,
        "action": canonical,
        "source_action": action,
        "tool_call_id": call_id,
        "observed_tool_event_ids": [tool.event_id for tool in matched],
        "dispatch_observation": "recorded_tool_span" if matched else "unknown",
        "dispatch_prevention": "unknown",
        "enforcement": "unverified",
        "source_enforcement_claim": attrs.get("policy.enforced")
        if type(attrs.get("policy.enforced")) is bool
        else None,
        "approval_id": _label(attrs.get("approval.id")),
        "approval_resolution": _label(attrs.get("approval.resolution")),
        "latency_ms": event.duration_ms if event.metadata.get("timing_available") is True else None,
        "basis": "external_reported_policy_decision",
    }


@dataclass(frozen=True)
class OmnigentImportResult:
    traces: tuple[AgentTrace, ...]
    source_receipts: tuple[ImportReceipt, ...]
    _inventory_json: str
    _sessions_json: str

    def inventory(self) -> dict:
        return json.loads(self._inventory_json)

    def sessions(self) -> dict:
        return json.loads(self._sessions_json)

    def write(self, out: str | Path) -> Path:
        inventory = write_import_bundle(self.traces, self.source_receipts, out, self.inventory())
        write_artifact(
            inventory.parent, "omnigent-sessions.json", (self._sessions_json + "\n").encode()
        )
        return inventory


def import_omnigent(
    path: str | Path,
    *,
    options: OtlpOptions | None = None,
    limits: ImportLimits = ImportLimits(),
    jsonl: bool | None = None,
) -> OmnigentImportResult:
    """Read source files only; no runtime, vendor tool or policy executes."""
    options = options or OtlpOptions(system="omnigent")
    if options.system != "omnigent":
        raise ImportValidationError(
            "invalid_source", "system", "Omnigent adaptation requires the omnigent source system"
        )
    imported = import_otlp(path, options=options, limits=limits, jsonl=jsonl)
    receipt_by_run = {
        item["run_id"]: receipt.to_dict()
        for receipt in imported.source_receipts
        for item in receipt.to_dict()["traces"]
    }
    span_index: dict[tuple[str, str], list[dict]] = {}
    for trace in imported.traces:
        for event in trace.events:
            if (
                event.metadata.get("source_trace_id") is not None
                and event.metadata.get("source_span_id") is not None
            ):
                key = (event.metadata["source_trace_id"], event.metadata["source_span_id"])
                span_index.setdefault(key, []).append(
                    {"run_id": trace.run_id, "event_id": event.event_id}
                )
    groups: dict[str, dict] = {}
    frozen = []
    for trace in imported.traces:
        receipt = receipt_by_run[trace.run_id]
        tools = [event for event in trace.events if _role(event) == "tool"]
        observations = []
        event_sessions = {}
        policies = []
        relations = []
        for event in trace.events:
            aliases = _aliases(event)
            ambiguous_aliases = [
                key
                for key, names in _ALIASES.items()
                if len(
                    {
                        _label(_attributes(event).get(name))
                        for name in names
                        if _label(_attributes(event).get(name)) is not None
                    }
                )
                > 1
            ]
            event.metadata["source.omnigent"] = {
                "schema_version": "1.0",
                **aliases,
                "observation_role": _role(event),
                "ambiguous_aliases": ambiguous_aliases,
                "capture_flags": {
                    key: value
                    for key, value in _attributes(event).items()
                    if key
                    in {
                        "content_capture_enabled",
                        "omnigent.capture_content",
                        "omnigent.tracing.content_capture",
                    }
                    and type(value) is bool
                },
            }
            observations.append(
                {
                    "event_id": event.event_id,
                    "source_span_id": event.metadata.get("source_span_id"),
                    **aliases,
                    "role": _role(event),
                }
            )
            event_sessions[event.event_id] = aliases["session_id"]
            if _role(event) == "guardrail" or "policy.action" in event.metadata:
                policies.append(_policy(event, tools))
            for link in event.metadata.get("otel_links", []):
                key = (link["traceId"].lower(), link["spanId"].lower())
                targets = span_index.get(key, [])
                relations.append(
                    {
                        "kind": "span_link",
                        "source_event_id": event.event_id,
                        "target_trace_id": key[0],
                        "target_span_id": key[1],
                        "targets": targets,
                        "resolved": len(targets) == 1,
                        "causal_edge": False,
                        "basis": "external_reported_link",
                    }
                )
            parent_reference = _label(event.metadata.get("parent_span_id"))
            if parent_reference is not None:
                relations.append(
                    {
                        "kind": "ended_parent_reference",
                        "source_event_id": event.event_id,
                        "target_trace_id": None,
                        "target_span_id": parent_reference,
                        "targets": [],
                        "resolved": False,
                        "causal_edge": False,
                        "basis": "external_reported_attribute_without_trace_scope",
                    }
                )
        model_count = sum(_role(event) == "model" for event in trace.events)
        summary = {
            "schema_version": "1.0",
            "adapter_contract_revision": CONTRACT_REVISION,
            "source_receipt_id": receipt["receipt_id"],
            "source_identity": receipt["external_identity"],
            "observation_count": len(observations),
            "agent_names": sorted(
                {item["agent_name"] for item in observations if item["agent_name"] is not None}
            ),
            "policy_decisions": policies,
            "relationships": relations,
            "timing_by_role": _timings(
                trace.events,
                conflicting=receipt["source_metadata"].get("conflicting_segments", 0) > 0,
            ),
            "model_coverage": "observed_partial" if model_count else "unknown",
            "task_correctness": "unavailable",
            "enforcement": "unverified",
            "source_execution_outcome": "unknown",
            "gaps": sorted(
                {
                    "complete_executor_model_coverage_unestablished",
                    "external_task_verifier_absent",
                    *(
                        ["unresolved_policy_ask"]
                        if any(
                            item["action"] == "ASK" and item["approval_resolution"] is None
                            for item in policies
                        )
                        else []
                    ),
                    *(
                        ["cross_process_parentage_incomplete"]
                        if any(not item["resolved"] for item in relations)
                        or any(
                            "unresolved_parent_span_id" in event.metadata for event in trace.events
                        )
                        else []
                    ),
                }
            ),
        }
        # Policy DENY has ERROR status in Omnigent. That is a recorded policy
        # decision, not an independently established failed task execution.
        trace.metadata[EXTERNAL_KEY]["execution_status"] = "unknown"
        attach_observations(trace, summary)
        receipt["outcome"]["execution_status"] = "unknown"
        receipt["source_metadata"]["omnigent"] = {
            key: summary[key]
            for key in (
                "schema_version",
                "adapter_contract_revision",
                "observation_count",
                "model_coverage",
                "task_correctness",
                "enforcement",
                "source_execution_outcome",
                "gaps",
            )
        }
        for item in receipt["traces"]:
            item["trace_sha256"] = sha256(native_bytes(trace)).hexdigest()
        frozen.append(ImportReceipt.from_dict(receipt, limits=limits))
        session_ids = {
            item["session_id"] for item in observations if item["session_id"] is not None
        }
        for session_id in sorted(session_ids):
            group_id = external_id(
                "group", "omnigent", {**options.identity, "session_id": session_id}
            )
            group = groups.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "session_id": session_id,
                    "run_ids": [],
                    "source_trace_ids": [],
                    "relationships": [],
                    "parentage": "partial",
                    "interpretation": "session grouping preserves separate trace identities and establishes no causal tree",
                },
            )
            group["run_ids"].append(trace.run_id)
            group["source_trace_ids"].append(receipt["external_identity"]["trace_id"])
            group["relationships"].extend(
                item
                for item in relations
                if event_sessions.get(item["source_event_id"]) == session_id
            )
    frozen.extend(
        receipt for receipt in imported.source_receipts if not receipt.to_dict()["traces"]
    )
    actions = Counter(
        item["action"]
        for trace in imported.traces
        for item in trace.metadata["agentloop.omnigent"]["observations"]["policy_decisions"]
    )
    inventory = {
        **imported.inventory(),
        "adapter": "omnigent-otel",
        "adapter_contract_revision": CONTRACT_REVISION,
        "session_groups": len(groups),
        "policy_actions": dict(sorted(actions.items())),
        "enforcement": "unverified",
        "model_coverage": "unknown_or_observed_partial",
        "session_sidecar": "omnigent-sessions.json",
    }
    sessions = {
        "schema_version": "1.0",
        "source": "omnigent",
        "groups": list(groups.values()),
        "unassigned_run_ids": [
            trace.run_id
            for trace in imported.traces
            if not any(trace.run_id in group["run_ids"] for group in groups.values())
        ],
    }
    inventory["session_sidecar_sha256"] = sha256(
        (canonical_json(sessions) + "\n").encode()
    ).hexdigest()
    return OmnigentImportResult(
        imported.traces, tuple(frozen), canonical_json(inventory), canonical_json(sessions)
    )


def inspect_omnigent_bundle(path: str | Path, limits: ImportLimits = ImportLimits()) -> dict:
    """Inspect existing exported evidence without executing any source records."""
    root = Path(path)
    inventory = load_json_artifact(root, "inventory.json", limits=limits).payload
    sessions_artifact = load_json_artifact(root, "omnigent-sessions.json", limits=limits)
    sessions = sessions_artifact.payload
    if (
        not isinstance(inventory, dict)
        or inventory.get("adapter") != "omnigent-otel"
        or not isinstance(sessions, dict)
        or sessions.get("schema_version") != "1.0"
        or sessions.get("source") != "omnigent"
        or inventory.get("session_sidecar_sha256") != sessions_artifact.artifact_sha256
    ):
        raise ImportValidationError(
            "invalid_bundle", "inventory", "not a supported Omnigent import bundle"
        )
    return {"inventory": inventory, "sessions": sessions}
