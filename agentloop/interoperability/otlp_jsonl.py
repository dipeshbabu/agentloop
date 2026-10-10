"""Bounded OTLP JSON/JSONL ingestion using AgentLoop's existing OTel parser."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentloop.interoperability.artifacts import native_bytes, write_import_bundle
from agentloop.interoperability.contracts import (
    IDENTITY_FIELDS,
    ImportReceipt,
    external_id,
    receipt_id,
    summarize_receipts,
)
from agentloop.interoperability.evidence import EXTERNAL_KEY
from agentloop.interoperability.otlp_validation import fail, flatten
from agentloop.interoperability.validation import (
    ImportBudget,
    ImportLimits,
    ImportValidationError,
    load_json_artifact,
    parse_json_bytes,
    safe_artifact_path,
)
from agentloop.interventions import canonical_json
from agentloop.otel import traces_from_otel
from agentloop.otel_semantics import INPUT_USAGE, OUTPUT_USAGE
from agentloop.schema import TraceValidationError
from agentloop.tracer import AgentTrace


@dataclass(frozen=True)
class OtlpOptions:
    system: str = "otel"
    producer_version: str | None = None
    producer_revision: str | None = None
    identity: dict[str, str] = field(default_factory=dict)
    continue_on_error: bool = True
    capture_content: bool = False
    synthetic_fixture: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.system, str) or self.system not in {"otel", "harbor", "omnigent"}:
            fail("system", "unsupported source system")
        for key in ("continue_on_error", "capture_content", "synthetic_fixture"):
            if type(getattr(self, key)) is not bool:
                fail("options", "import flags must be boolean")
        if not isinstance(self.identity, dict) or set(self.identity) - (
            IDENTITY_FIELDS - {"trace_id", "trajectory_id", "session_id"}
        ):
            fail("identity", "unsupported caller identity fields")
        for value in (*self.identity.values(), self.producer_version):
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value.encode()) > 512
            ):
                fail("identity", "identities/versions must be bounded strings")
        if self.producer_revision is not None and (
            not isinstance(self.producer_revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", self.producer_revision)
        ):
            fail("producer_revision", "revision must be a full lowercase Git SHA")


@dataclass(frozen=True)
class OtlpImportResult:
    traces: tuple[AgentTrace, ...]
    source_receipts: tuple[ImportReceipt, ...]
    _inventory_json: str

    def inventory(self) -> dict:
        return json.loads(self._inventory_json)

    def write(self, out: str | Path) -> Path:
        return write_import_bundle(self.traces, self.source_receipts, out, self.inventory())


class _BatchImporter:
    def __init__(
        self, root: Path, reference: str, options: OtlpOptions, limits: ImportLimits, jsonl: bool
    ):
        self.root, self.reference, self.options, self.limits, self.jsonl = (
            root,
            reference,
            options,
            limits,
            jsonl,
        )
        self.budget = ImportBudget(limits)
        self.file_hash = sha256()
        self.artifact_sha256 = ""
        self.groups: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.records: list[dict] = []
        self.blank_lines = 0
        self.raw_spans = self.raw_logs = self.duplicates = self.conflicts = 0

    def notice(
        self,
        code: str,
        line: int | None = None,
        field: str | None = None,
        severity: str = "warning",
    ) -> dict:
        return {
            "code": code,
            "severity": severity,
            "artifact": self.reference,
            "field": field,
            "record": line,
        }

    def invalid(self, exc: ImportValidationError, line: int) -> None:
        if not self.options.continue_on_error:
            raise exc
        self.errors.append(self.notice(exc.code, line, exc.field, "error"))
        self.records.append({"record": line, "status": "invalid", "diagnostic": exc.to_dict()})

    def read(self) -> None:
        if not self.jsonl:
            try:
                artifact = load_json_artifact(
                    self.root, self.reference, limits=self.limits, budget=self.budget
                )
                self.artifact_sha256 = artifact.artifact_sha256
                self.record(artifact.payload, 1, artifact.artifact_sha256)
            except ImportValidationError as exc:
                if exc.code == "limit_exceeded":
                    raise
                self.artifact_sha256 = getattr(exc, "artifact_sha256", "")
                if not self.artifact_sha256:
                    raise
                self.invalid(exc, 1)
            return
        path = safe_artifact_path(self.root, self.reference, self.limits)
        with path.open("rb") as stream:
            line = 0
            while True:
                remaining = self.limits.max_total_bytes - self.budget.total_bytes
                data = stream.readline(min(self.limits.max_line_bytes, remaining) + 1)
                if not data:
                    break
                line += 1
                self.budget.consume(total_bytes=len(data), records=1)
                self.file_hash.update(data)
                line_hash = sha256(data)
                oversized = len(data) > self.limits.max_line_bytes
                # Drain an oversized record in bounded chunks. It remains one
                # physical record; total byte overflow is always terminal.
                while oversized and not data.endswith(b"\n"):
                    remaining = self.limits.max_total_bytes - self.budget.total_bytes
                    data = stream.readline(min(self.limits.max_line_bytes, remaining) + 1)
                    if not data:
                        break
                    self.budget.consume(total_bytes=len(data))
                    self.file_hash.update(data)
                    line_hash.update(data)
                if oversized:
                    self.invalid(
                        ImportValidationError(
                            "record_limit_exceeded",
                            "line.bytes",
                            "JSONL line exceeds configured bounds",
                        ),
                        line,
                    )
                    continue
                if not data.strip():
                    self.blank_lines += 1
                    continue
                try:
                    payload = parse_json_bytes(data, self.limits)
                    self.record(payload, line, line_hash.hexdigest())
                except ImportValidationError as exc:
                    if exc.code == "limit_exceeded" and exc.field in {
                        "total_bytes",
                        "records",
                        "spans",
                        "trajectories",
                        "references",
                        "events",
                    }:
                        raise
                    self.invalid(exc, line)
        if safe_artifact_path(self.root, self.reference, self.limits) != path:
            fail("reference", "artifact path changed while reading", "unsafe_path")
        self.artifact_sha256 = self.file_hash.hexdigest()

    def record(self, payload: Any, line: int, record_hash: str) -> None:
        try:
            clean, spans, logs = flatten(
                payload, self.limits, capture_content=self.options.capture_content
            )
            self.budget.consume(spans=len(spans) + len(logs))
            self.budget.consume(
                references=sum(len(item["span"].get("links", [])) for item in spans)
            )
            self.raw_spans += len(spans)
            self.raw_logs += len(logs)
            witnesses: dict[tuple[str, str], dict] = {}
            by_group: dict[str, list[dict]] = {}
            for index, (is_span, witness) in enumerate(
                [(True, item) for item in spans] + [(False, item) for item in logs]
            ):
                group = (
                    witness["trace_id"]
                    or sha256(
                        canonical_json({"record": line, "sha256": record_hash}).encode()
                    ).hexdigest()[:32]
                )
                item = witness["span"]
                item["traceId"] = group
                if not witness["span_id"]:
                    item["spanId"] = sha256(
                        canonical_json(
                            {"record": line, "ordinal": index, "sha256": record_hash}
                        ).encode()
                    ).hexdigest()[:16]
                else:
                    item["spanId"] = witness["span_id"]
                witness.update(group=group, record=line, record_sha256=record_hash)
                by_group.setdefault(group, []).append(witness)
                if is_span:
                    key = (group, item["spanId"])
                    if (
                        key in witnesses
                        and witnesses[key]["source_sha256"] != witness["source_sha256"]
                    ):
                        fail("spanId", "conflicting span identity within one record")
                    witnesses[key] = witness
            # All span/log interpretation belongs to the existing parser. We
            # qualify its native projection rather than building another parser.
            projected = traces_from_otel(clean)
            pending = []
            for trace in projected:
                group_ids = {event.metadata.get("otel_trace_id") for event in trace.events}
                if not group_ids:
                    group_ids = {trace.run_id.removeprefix("run_")}
                if len(group_ids) != 1:
                    fail("traceId", "native projection has ambiguous trace identity")
                group = next(iter(group_ids))
                source_witnesses = by_group.get(group, [])
                if not source_witnesses:
                    fail("traceId", "native identity does not match source telemetry")
                for event in trace.events:
                    witness = witnesses[(group, event.metadata["otel_span_id"])]
                    self.qualify_event(event, witness)
                    if (
                        len(canonical_json(event.metadata).encode())
                        > self.limits.max_metadata_bytes
                    ):
                        fail(
                            "span.metadata",
                            "projected metadata exceeds configured bounds",
                            "record_limit_exceeded",
                        )
                if len(trace.events) > self.limits.max_events_per_trace:
                    fail("events", "event count exceeds configured bounds", "record_limit_exceeded")
                pending.append((group, trace, source_witnesses, witnesses))
            new_groups = {group for group, *_ in pending} - set(self.groups)
            self.budget.consume(trajectories=len(new_groups))
            for group, trace, source_witnesses, witnesses in pending:
                self.merge(group, trace, source_witnesses, witnesses)
            self.records.append(
                {
                    "record": line,
                    "status": "imported",
                    "sha256": record_hash,
                    "trace_groups": [item[0] for item in pending],
                }
            )
        except TraceValidationError:
            fail("record", "OTLP semantic validation failed")
        except (OverflowError, TypeError, KeyError, AttributeError, ValueError) as exc:
            if isinstance(exc, ImportValidationError):
                raise
            fail("record", "OTLP structural or semantic validation failed")

    def qualify_event(self, event: Any, witness: dict) -> None:
        attrs, span = witness["attributes"], witness["span"]
        converter = (
            witness["resource"].get("telemetry.sdk.name") == "harbor-atif2otel"
            or witness["scope"].get("name") == "harbor-atif2otel"
        )
        event.metadata["otel_parent_span_id"] = (span.get("parentSpanId") or "").lower() or None
        event.metadata["source_interval"] = {
            "start_unix_nano": span.get("startTimeUnixNano"),
            "end_unix_nano": span.get("endTimeUnixNano"),
            "duration_ms": event.duration_ms,
        }
        event.metadata["unrepresented_structural_fields"] = witness["unrepresented_fields"]
        native_identity = {
            "event_id": attrs.get("agentloop.native_event_id"),
            "parent_id": attrs.get("agentloop.native_parent_id"),
            "run_id": witness["resource"].get("agentloop.run_id") or attrs.get("agentloop.run_id"),
        }
        if any(
            value is not None and (not isinstance(value, str) or len(value.encode()) > 512)
            for value in native_identity.values()
        ):
            fail(
                "agentloop.native_identity",
                "native identity assertions must be bounded strings or null",
            )
        if any(value is not None for value in native_identity.values()):
            event.metadata["source_native_identity"] = native_identity
        start, end = span.get("startTimeUnixNano"), span.get("endTimeUnixNano")
        if start is None:
            event.started_at = "unknown"
        if end is None:
            event.ended_at = "unknown"
        timing = start is not None and end is not None and not converter
        if timing:
            try:
                timing = (
                    type(start) in {int, str}
                    and type(end) in {int, str}
                    and int(end) >= int(start) >= 0
                )
            except (ValueError, OverflowError):
                timing = False
        status = span.get("status", {}).get("code")
        status_available = status in {1, 2, "STATUS_CODE_OK", "STATUS_CODE_ERROR"} and not converter
        if event.operation_kind == "unknown" or not any(
            isinstance(attrs.get(key), str) and bool(attrs[key].strip())
            for key in (
                "gen_ai.operation.name",
                "openinference.span.kind",
                "mcp.method.name",
                "agentloop.event_type",
            )
        ):
            event.event_type = "source_span"
            event.metadata["operation_kind"] = "unknown"
        if not timing:
            event.duration_ms = 0.0
        input_available = any(key in attrs for key in ("agentloop.input_tokens", *INPUT_USAGE))
        output_available = any(key in attrs for key in ("agentloop.output_tokens", *OUTPUT_USAGE))
        event.metadata.update(
            external_evidence_schema="1.0",
            timing_available=bool(timing),
            timing_provenance="inferred"
            if converter
            else "external_reported"
            if timing
            else "unknown",
            source_status_available=status_available,
            source_execution_status="failed"
            if status in {2, "STATUS_CODE_ERROR"} and not converter
            else "completed"
            if status_available
            else "unknown",
            input_tokens_available=input_available,
            output_tokens_available=output_available,
            source_token_provenance=event.token_provenance,
            source_span_sha256=witness["source_sha256"],
            source_span_id=witness["span_id"],
            source_trace_id=witness["trace_id"],
            source_record=witness["record"],
            converter_generated=converter,
        )
        if event.event_type == "model_call":
            multiplicity = attrs.get("metadata.aggregated_llm_calls", 1)
            if type(multiplicity) is not int or multiplicity < 1:
                fail(
                    "metadata.aggregated_llm_calls",
                    "model block multiplicity must be a positive integer",
                )
            event.metadata["source_model_call_count"] = multiplicity
            event.token_provenance = (
                "external_reported" if input_available and output_available else "unavailable"
            )
        cache = event.metadata.get("cached_input_tokens")
        if cache is not None and input_available and cache > event.input_tokens:
            fail("usage", "cache usage must be a prompt subset")

    def merge(
        self, group: str, trace: AgentTrace, witnesses: list[dict], span_witnesses: dict
    ) -> None:
        state = self.groups.setdefault(
            group,
            {
                "events": {},
                "trace_id": witnesses[0]["trace_id"],
                "records": [],
                "notices": [],
                "logs": [],
                "sessions": set(),
                "bounds": [],
                "conflicts": 0,
                "source_aggregates": [],
            },
        )
        state["records"].append(witnesses[0]["record"])
        for event in trace.events:
            state["logs"].extend(event.metadata.get("otel_log_records", []))
        for witness in witnesses:
            for source_attrs in (witness["attributes"], witness["resource"]):
                session = source_attrs.get("session.id") or source_attrs.get(
                    "gen_ai.conversation.id"
                )
                if isinstance(session, str) and session.strip() and len(session.encode()) <= 512:
                    state["sessions"].add(session)
        for event in trace.events:
            witness = span_witnesses[(group, event.metadata["otel_span_id"])]
            span_id = event.metadata["otel_span_id"]
            existing = state["events"].get(span_id)
            if existing is not None:
                if existing.metadata["source_span_sha256"] == event.metadata["source_span_sha256"]:
                    self.duplicates += 1
                    state["notices"].append(
                        self.notice("identical_span_duplicate", witness["record"], "spanId", "info")
                    )
                else:
                    self.conflicts += 1
                    state["conflicts"] += 1
                    state["notices"].append(
                        self.notice(
                            "conflicting_span_segment", witness["record"], "spanId", "error"
                        )
                    )
                continue
            if len(state["events"]) >= self.limits.max_events_per_trace:
                fail("events", "merged trace exceeds configured bounds", "limit_exceeded")
            event.metadata.pop("agentloop.external", None)
            state["events"][span_id] = event
            if event.metadata["unrepresented_structural_fields"]:
                state["notices"].append(
                    self.notice("unrepresented_structural_fields", witness["record"], "record")
                )
            if event.operation_kind != "model" and any(
                key in witness["attributes"]
                for key in (*INPUT_USAGE, *OUTPUT_USAGE, "llm.cost.total")
            ):
                state["source_aggregates"].append(
                    {
                        "span_id": span_id,
                        "input_tokens": event.input_tokens
                        if event.metadata["input_tokens_available"]
                        else None,
                        "output_tokens": event.output_tokens
                        if event.metadata["output_tokens_available"]
                        else None,
                        "cost_usd": event.metadata.get("provider_reported_cost_usd"),
                        "basis": "external_reported_nonmodel_aggregate",
                    }
                )
            if event.metadata["timing_available"]:
                state["bounds"].append(
                    (
                        int(witness["span"]["startTimeUnixNano"]),
                        int(witness["span"]["endTimeUnixNano"]),
                    )
                )
            if event.metadata["converter_generated"]:
                state["notices"].append(
                    self.notice("converter_inferred_timing_status", witness["record"], "timing")
                )
        log_records = trace.metadata.get("otel_log_records", [])
        state["logs"].extend(log_records)

    def receipt(
        self,
        identity: dict,
        *,
        reason: str | None,
        notices: list[dict],
        metadata: dict,
        traces: list[dict] | None = None,
        completeness: dict | None = None,
        execution: str = "unknown",
    ) -> dict:
        return {
            "schema_version": "1.0",
            "receipt_id": "pending",
            "source": {
                "system": self.options.system,
                "producer_version": self.options.producer_version,
                "producer_revision": self.options.producer_revision,
                "format": "otlp_jsonl" if self.jsonl else "otlp_json",
                "format_version": None,
                "artifact_reference": self.reference,
                "artifact_sha256": self.artifact_sha256,
                "trust": "external_reported",
            },
            "external_identity": identity,
            "identity_provenance": {
                key: "calculated"
                if key == "trajectory_id" and value is not None
                else "external_reported"
                if value is not None
                else "unknown"
                for key, value in identity.items()
            },
            "traces": traces or [],
            "missing_trace_reason": reason,
            "outcome": {
                "execution_status": execution,
                "verifier_status": "unknown",
                "verifier_dimensions": {},
                "quality_pass": None,  # nosec B105
                "quality_basis": "unknown",
                "scoring_contract": None,
                "verifier_isolation": "unknown",
            },
            "completeness": completeness
            or dict.fromkeys(
                ("trajectory", "usage", "quality", "parentage", "timing", "cost"), "unknown"
            ),
            "relationships": [],
            "notices": notices,
            "source_metadata": metadata,
        }

    def finish(self) -> OtlpImportResult:
        traces, receipts = [], []
        for group, state in self.groups.items():
            identity = {
                **dict.fromkeys(sorted(IDENTITY_FIELDS)),
                **self.options.identity,
                "trace_id": state["trace_id"],
            }
            if state["trace_id"] is None:
                identity["trajectory_id"] = external_id(
                    "group",
                    self.options.system,
                    {
                        "artifact_reference": self.reference,
                        "artifact_sha256": self.artifact_sha256,
                        "source_record": state["records"][0],
                    },
                )
                state["notices"].append(
                    self.notice("missing_trace_identity", state["records"][0], "traceId")
                )
            if len(state["sessions"]) == 1:
                identity["session_id"] = next(iter(state["sessions"]))
            elif len(state["sessions"]) > 1:
                state["notices"].append(self.notice("multiple_trace_sessions", field="session.id"))
            events = list(state["events"].values())
            cyclic = _cyclic_parents(state["events"])
            models = [event for event in events if event.event_type == "model_call"]
            timing = (
                bool(events)
                and state["trace_id"] is not None
                and all(event.metadata["timing_available"] for event in events)
                and not state["conflicts"]
                and not cyclic
            )
            usage = (
                bool(models)
                and all(
                    event.metadata["input_tokens_available"]
                    and event.metadata["output_tokens_available"]
                    for event in models
                )
                and not state["conflicts"]
            )
            runtime = (
                (
                    max(end for _, end in state["bounds"])
                    - min(start for start, _ in state["bounds"])
                )
                / 1_000_000
                if timing
                else None
            )
            execution = (
                "failed"
                if any(
                    event.status == "error" and event.metadata["source_status_available"]
                    for event in events
                )
                else "unknown"
            )
            metadata = {
                "records": sorted(set(state["records"])),
                "raw_unique_spans": len(events),
                "conflicting_segments": state["conflicts"],
                "source_aggregates": state["source_aggregates"],
                "source_logs": state["logs"],
                "coverage_basis": "exported telemetry; source execution/usage completeness not established",
                "atif_losses": [
                    "original interaction/context history",
                    "source timestamps vs generated intervals",
                    "source execution outcome",
                    "unconverted or unresolved documents",
                ]
                if self.options.system == "harbor"
                or any(event.metadata["converter_generated"] for event in events)
                else [],
            }
            receipt = self.receipt(
                identity,
                reason="no_execution_spans" if not events else None,
                notices=state["notices"],
                metadata=metadata,
                completeness={
                    "trajectory": "partial" if events else "missing",
                    "usage": "complete" if usage else "partial" if models else "unknown",
                    "quality": "unknown",
                    "parentage": "unknown",
                    "timing": "complete" if timing else "missing",
                    "cost": "unknown",
                },
                execution=execution,
            )
            receipt["receipt_id"] = receipt_id(receipt)
            if events:
                run_id = external_id(
                    "run", self.options.system, {**identity, "source_group": group}
                )
                event_ids = {
                    span_id: external_id(
                        "event", self.options.system, {"run_id": run_id, "span_id": span_id}
                    )
                    for span_id in state["events"]
                }
                trace = AgentTrace(
                    name="external_otel_trace",
                    run_id=run_id,
                    started_at=events[0].started_at,
                    elapsed_ms=runtime,
                    metadata={
                        "source": self.options.system,
                        **({"synthetic": True} if self.options.synthetic_fixture else {}),
                    },
                )
                for event in events:
                    parent_span = event.metadata.get("otel_parent_span_id")
                    event.run_id, event.event_id = run_id, event_ids[event.metadata["otel_span_id"]]
                    source_parent = state["events"].get(parent_span)
                    event.parent_id = (
                        event_ids.get(parent_span)
                        if state["trace_id"] is not None
                        and source_parent is not None
                        and source_parent.metadata["source_span_id"] is not None
                        else None
                    )
                    if event.metadata["source_span_id"] is None:
                        receipt["notices"].append(
                            self.notice(
                                "missing_span_identity", event.metadata["source_record"], "spanId"
                            )
                        )
                    if event.metadata["otel_span_id"] in cyclic:
                        event.parent_id = None
                        event.metadata["unresolved_parent_span_id"] = parent_span
                        receipt["notices"].append(
                            self.notice(
                                "cyclic_parentage",
                                event.metadata["source_record"],
                                "parentSpanId",
                                "error",
                            )
                        )
                    if parent_span is not None and event.parent_id is None:
                        event.metadata["unresolved_parent_span_id"] = parent_span
                        receipt["notices"].append(
                            self.notice(
                                "unresolved_parent_span",
                                event.metadata["source_record"],
                                "parentSpanId",
                            )
                        )
                    event.metadata["source_receipt_id"] = receipt["receipt_id"]
                trace.events = events
                trace.metadata[EXTERNAL_KEY] = {
                    "schema_version": "1.0",
                    "source": {
                        "system": self.options.system,
                        "format": receipt["source"]["format"],
                    },
                    "receipt_id": receipt["receipt_id"],
                    "runtime_ms": runtime,
                    "event_timing_complete": timing,
                    "execution_status": execution,
                    "usage_complete": usage,
                    "reported_model_call_count": sum(
                        event.metadata["source_model_call_count"] for event in models
                    )
                    if models
                    else None,
                    "comparison_eligible": False,
                }
                receipt["traces"] = [
                    {
                        "run_id": run_id,
                        "trace_file": f"traces/{run_id}.json",
                        "trace_sha256": sha256(native_bytes(trace)).hexdigest(),
                    }
                ]
                traces.append(trace)
            receipts.append(ImportReceipt.from_dict(receipt, limits=self.limits))
        if self.errors or not self.groups:
            identity = {**dict.fromkeys(sorted(IDENTITY_FIELDS)), **self.options.identity}
            receipt = self.receipt(
                identity,
                reason="invalid_artifact" if self.errors else "no_execution_spans",
                notices=self.errors,
                metadata={
                    "invalid_record_count": len(self.errors),
                    "blank_line_count": self.blank_lines,
                },
            )
            receipt["receipt_id"] = receipt_id(receipt)
            receipts.append(ImportReceipt.from_dict(receipt, limits=self.limits))
        inventory = {
            "schema_version": "1.0",
            "source_system": self.options.system,
            "artifact_reference": self.reference,
            "artifact_sha256": self.artifact_sha256,
            "source_format": "otlp_jsonl" if self.jsonl else "otlp_json",
            "physical_records": self.budget.records,
            "blank_lines": self.blank_lines,
            "valid_records": sum(row["status"] == "imported" for row in self.records),
            "invalid_records": len(self.errors),
            "raw_spans": self.raw_spans,
            "raw_log_records": self.raw_logs,
            "identical_duplicate_spans": self.duplicates,
            "conflicting_span_segments": self.conflicts,
            "trace_groups": len(self.groups),
            "record_rows": self.records,
            **summarize_receipts(receipts),
        }
        return OtlpImportResult(tuple(traces), tuple(receipts), canonical_json(inventory))


def _cyclic_parents(events: dict[str, Any]) -> set[str]:
    """Find cycles in a source parent forest without recursion or invented edges."""
    parents = {key: event.metadata.get("otel_parent_span_id") for key, event in events.items()}
    done, cyclic = set(), set()
    for start in parents:
        current, path, positions = start, [], {}
        while current in parents and current not in done:
            if current in positions:
                cyclic.update(path[positions[current] :])
                break
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        done.update(path)
    return cyclic


def import_otlp(
    path: str | Path,
    *,
    root: str | Path | None = None,
    options: OtlpOptions = OtlpOptions(),
    limits: ImportLimits = ImportLimits(),
    jsonl: bool | None = None,
) -> OtlpImportResult:
    """Import a bounded file offline; malformed independent records stay visible."""
    if jsonl is not None and type(jsonl) is not bool:
        fail("jsonl", "format selection must be boolean or null")
    selected = Path(path).absolute()
    source_root = Path(root).absolute() if root is not None else selected.parent
    try:
        reference = selected.relative_to(source_root).as_posix()
    except ValueError:
        fail("path", "input lies outside the selected source root", "unsafe_path")
    importer = _BatchImporter(
        source_root.resolve(),
        reference,
        options,
        limits,
        selected.suffix.lower() == ".jsonl" if jsonl is None else jsonl,
    )
    importer.read()
    return importer.finish()
