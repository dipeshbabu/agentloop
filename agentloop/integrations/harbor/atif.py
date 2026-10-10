"""Read-only ATIF import with qualified native events and immutable receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentloop.events import AgentEvent
from agentloop.integrations.harbor.atif_validation import (
    fail,
    label,
    timestamp,
    validate_trajectory,
)
from agentloop.interoperability.contracts import (
    IDENTITY_FIELDS,
    ImportReceipt,
    external_id,
    receipt_id,
    summarize_receipts,
)
from agentloop.interoperability.evidence import EXTERNAL_KEY
from agentloop.interoperability.validation import (
    ImportBudget,
    ImportLimits,
    ImportValidationError,
    JsonArtifact,
    indirect_path,
    load_json_artifact,
    relative_reference,
)
from agentloop.interventions import canonical_json
from agentloop.tracer import AgentTrace

ATIF_CONTRACT_REVISION = "07ad34000e4c481451b1a0ea30a4a09548d6de8b"


@dataclass(frozen=True)
class AtifOptions:
    capture_content: bool = False
    capture_reasoning: bool = False
    capture_token_ids: bool = False
    strict: bool = True
    synthetic_fixture: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in vars(self).values()):
            fail("options", "capture and validation options must be boolean")


def _native_bytes(trace: AgentTrace) -> bytes:
    return (
        json.dumps(trace.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class AtifImportResult:
    traces: tuple[AgentTrace, ...]
    source_receipts: tuple[ImportReceipt, ...]

    @property
    def warnings(self) -> tuple[dict, ...]:
        return tuple(
            notice for receipt in self.source_receipts for notice in receipt.to_dict()["notices"]
        )

    def inventory(self) -> dict:
        return {
            "schema_version": "1.0",
            "source_format": "atif",
            **summarize_receipts(list(self.source_receipts)),
            "invalid_documents": sum(
                receipt.to_dict()["missing_trace_reason"] == "invalid_artifact"
                for receipt in self.source_receipts
            ),
            "timing_unavailable": sum(
                receipt.to_dict()["completeness"]["timing"] != "complete"
                for receipt in self.source_receipts
            ),
        }

    def write(self, out: str | Path) -> Path:
        """Write stable native files and receipts; conflicting bytes are an error."""
        expected = {
            item["run_id"]: item["trace_sha256"]
            for receipt in self.source_receipts
            for item in receipt.to_dict()["traces"]
        }
        for trace in self.traces:
            if sha256(_native_bytes(trace)).hexdigest() != expected.get(trace.run_id):
                fail("native_trace", "trace changed after receipt capture", "source_conflict")
        root = Path(out)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()

        def write(reference: str, data: bytes) -> None:
            relative_reference(reference)
            target = root / reference
            current = root
            for part in reference.split("/"):
                current = current / part
                try:
                    indirect = indirect_path(current)
                except FileNotFoundError:
                    indirect = False
                if indirect:
                    fail("output", "output references cannot be symlinks", "unsafe_path")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != data:
                    fail("output", "existing artifact has conflicting content", "output_conflict")
                return
            with target.open("xb") as stream:
                stream.write(data)

        for trace in self.traces:
            write(f"traces/{trace.run_id}.json", _native_bytes(trace))
        for receipt in self.source_receipts:
            write(
                f"receipts/{receipt.receipt_id}.json",
                (canonical_json(receipt.to_dict()) + "\n").encode(),
            )
        write("inventory.json", (canonical_json(self.inventory()) + "\n").encode())
        return root / "inventory.json"


class _Importer:
    def __init__(
        self, root: Path, limits: ImportLimits, options: AtifOptions, identity: dict[str, str]
    ):
        self.root, self.limits, self.options, self.identity = root, limits, options, identity
        self.budget = ImportBudget(limits)
        self.traces: list[AgentTrace] = []
        self.receipts: list[ImportReceipt] = []
        self.active_files: set[str] = set()
        self.completed_files: dict[str, str] = {}
        self.document_ids: set[str] = set()
        self.partial_documents: set[str] = set()

    def notice(
        self,
        notices: list[dict],
        code: str,
        artifact: JsonArtifact,
        field: str | None = None,
        severity: str = "warning",
    ) -> None:
        notices.append(
            {
                "code": code,
                "severity": severity,
                "artifact": artifact.reference,
                "field": field,
                "record": None,
            }
        )

    def text(self, value: str, *, capture: bool) -> Any:
        size = len(value.encode("utf-8"))
        if capture and size <= self.limits.max_metadata_bytes:
            return value
        return {
            "content_available": True,
            "size_bytes": size,
            "capture": "omitted",
            "reason": "size_limit" if capture else "capture_disabled",
            "provenance": "external_reported",
        }

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value, capture=self.options.capture_content)
        if isinstance(value, dict):
            result = {}
            safe_labels = {
                "usage_scope": {
                    "includes_subagents",
                    "self_only",
                    "agent",
                    "subagents",
                    "agent_and_subagents",
                    "model_calls",
                    "trial",
                    "unknown",
                },
                "cost_scope": {
                    "includes_subagents",
                    "self_only",
                    "agent",
                    "subagents",
                    "agent_and_subagents",
                    "model_calls",
                    "trial",
                    "unknown",
                },
                "context_operation": {"summarize", "compact", "truncate", "prune"},
                "accounting_source": {
                    "provider_reported",
                    "external_reported",
                    "calculated",
                    "unknown",
                },
            }
            for key, item in value.items():
                normalized = key.lower().replace("-", "_")
                compact = normalized.replace("_", "")
                token_ids = "token_ids" in normalized or normalized == "logprobs"
                sensitive = (
                    any(
                        word in compact
                        for word in (
                            "password",
                            "passwd",
                            "authorization",
                            "apikey",
                            "secret",
                            "credential",
                            "bearer",
                            "cookie",
                            "privatekey",
                        )
                    )
                    or "token" in compact
                    and not token_ids
                    and normalized
                    not in {
                        "prompt_tokens",
                        "completion_tokens",
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "max_tokens",
                        "total_tokens",
                    }
                )
                reasoning = any(word in normalized for word in ("reasoning", "chain_of_thought"))
                if (
                    sensitive
                    or reasoning
                    and not self.options.capture_reasoning
                    or token_ids
                    and not self.options.capture_token_ids
                ):
                    result[key] = {
                        "capture": "omitted",
                        "reason": "sensitive_field",
                        "size_bytes": len(canonical_json(item).encode()),
                    }
                elif isinstance(item, str) and item in safe_labels.get(key, set()):
                    result[key] = item
                else:
                    result[key] = self.sanitize(item)
            return result
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        return value

    def bounded(self, value: Any, notices: list[dict], artifact: JsonArtifact, field: str) -> Any:
        sanitized = self.sanitize(value)
        if len(canonical_json(sanitized).encode()) > self.limits.max_metadata_bytes:
            self.notice(notices, "source_metadata_dropped", artifact, field)
            return {"capture": "omitted", "reason": "metadata_size_limit"}
        return sanitized

    def arguments(
        self, value: dict, notices: list[dict], artifact: JsonArtifact, field: str
    ) -> dict:
        if self.options.capture_content:
            return self.bounded(value, notices, artifact, field)
        return {
            "capture": "omitted",
            "size_bytes": len(canonical_json(value).encode()),
            "provenance": "external_reported",
        }

    def reference_metadata(
        self, reference: dict, notices: list[dict], artifact: JsonArtifact, field: str
    ) -> dict:
        result = {
            "trajectory_id": reference.get("trajectory_id"),
            "session_id": reference.get("session_id"),
            "extra": self.bounded(reference.get("extra"), notices, artifact, field),
        }
        path = reference.get("trajectory_path")
        if path is not None:
            try:
                result["trajectory_path"] = relative_reference(path, self.limits)
            except ImportValidationError:
                result["trajectory_path_sha256"] = sha256(path.encode()).hexdigest()
                result["path_capture"] = "omitted"
        return result

    def content(
        self,
        value: Any,
        notices: list[dict],
        artifact: JsonArtifact,
        field: str,
        *,
        consume_reference: bool = True,
    ) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return self.text(value, capture=self.options.capture_content)
        parts = []
        for part in value:
            if part["type"] == "text":
                parts.append(
                    {
                        "type": "text",
                        "text": self.text(part["text"], capture=self.options.capture_content),
                    }
                )
            else:
                if consume_reference:
                    self.budget.consume(references=1)
                source = part["source"]
                marker = {"type": part["type"], "media_type": source["media_type"], "loaded": False}
                try:
                    marker["reference"] = relative_reference(source["path"], self.limits)
                except ImportValidationError:
                    marker["reference_sha256"] = sha256(source["path"].encode()).hexdigest()
                    marker["reference_omitted"] = True
                    self.notice(notices, "external_media_reference_omitted", artifact, field)
                if "duration_sec" in source:
                    marker["source_duration_sec"] = source["duration_sec"]
                parts.append(marker)
        return parts

    def interval(
        self, extra: dict | None, notices: list[dict], artifact: JsonArtifact, field: str
    ) -> tuple[str, str, float] | None:
        declaration = (extra or {}).get("agentloop")
        if declaration is None:
            return None
        if not isinstance(declaration, dict):
            fail(field + ".agentloop", "measurement extension must be an object")
        timing = declaration.get("timing")
        if timing is None:
            return None
        if not isinstance(timing, dict) or set(timing) - {"started_at", "ended_at", "duration_ms"}:
            fail(field + ".timing", "invalid timing extension")
        if timing.get("started_at") is None or timing.get("ended_at") is None:
            self.notice(notices, "incomplete_source_interval", artifact, field)
            return None
        start = timestamp(timing["started_at"], field + ".timing.started_at")
        end = timestamp(timing["ended_at"], field + ".timing.ended_at")
        if start.tzinfo is None or end.tzinfo is None:
            self.notice(notices, "timestamp_timezone_unknown", artifact, field)
            return None
        duration = (end - start).total_seconds() * 1000
        if duration < 0:
            fail(field + ".timing", "source interval ends before it begins")
        given = timing.get("duration_ms")
        if given is not None and (type(given) not in {int, float} or abs(given - duration) > 0.001):
            fail(field + ".timing", "duration contradicts source interval")
        return timing["started_at"], timing["ended_at"], duration

    def status(self, extra: dict | None) -> str:
        declaration = (extra or {}).get("agentloop") or {}
        if not isinstance(declaration, dict):
            fail("extra.agentloop", "measurement extension must be an object")
        value = declaration.get("execution_status", "unknown")
        if not isinstance(value, str) or value not in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "partial",
            "missing",
            "unknown",
        }:
            fail("extra.agentloop.execution_status", "unsupported source execution status")
        return value

    def identity_for(
        self, payload: dict, artifact: JsonArtifact, locator: str, inherited_session: str | None
    ) -> tuple[dict, dict]:
        identity = dict.fromkeys(sorted(IDENTITY_FIELDS))
        identity.update(self.identity)
        identity.update(
            session_id=payload.get("session_id") or inherited_session,
            trajectory_id=payload.get("trajectory_id"),
        )
        provenance = {
            key: "external_reported" if value is not None else "unknown"
            for key, value in identity.items()
        }
        if payload.get("session_id") is None and inherited_session is not None:
            provenance["session_id"] = "inferred"
        if identity["trajectory_id"] is None:
            identity["trajectory_id"] = external_id(
                "group",
                "harbor",
                {
                    "artifact_sha256": artifact.artifact_sha256,
                    "artifact_reference": artifact.reference,
                    "locator": locator,
                },
            )
            provenance["trajectory_id"] = "calculated"
        return identity, provenance

    def base_receipt(
        self, payload: dict, artifact: JsonArtifact, locator: str, inherited_session: str | None
    ) -> dict:
        identity, provenance = self.identity_for(payload, artifact, locator, inherited_session)
        version = payload.get("schema_version")
        version = (
            version
            if isinstance(version, str) and version and len(version.encode()) <= 512
            else None
        )
        return {
            "schema_version": "1.0",
            "receipt_id": "pending",
            "source": {
                "system": "harbor",
                "producer_version": None,
                "producer_revision": None,
                "format": "atif",
                "format_version": version,
                "artifact_reference": artifact.reference,
                "artifact_sha256": artifact.artifact_sha256,
                "trust": "agent_writable",
            },
            "external_identity": identity,
            "identity_provenance": provenance,
            "traces": [],
            "missing_trace_reason": "invalid_artifact",
            "outcome": {
                "execution_status": "unknown",
                "verifier_status": "missing",
                "verifier_dimensions": {},
                **dict.fromkeys(("quality_pass",)),
                "quality_basis": "unavailable",
                "scoring_contract": None,
                "verifier_isolation": "unknown",
            },
            "completeness": {
                "trajectory": "unknown",
                "usage": "unknown",
                "quality": "missing",
                "parentage": "unknown",
                "timing": "unknown",
                "cost": "unknown",
            },
            "relationships": [],
            "notices": [],
            "source_metadata": {"document_locator": locator},
        }

    def file(self, reference: str, depth: int = 0, session: str | None = None) -> str | None:
        if reference in self.active_files:
            fail("reference", "cyclic trajectory reference", "reference_cycle")
        if reference in self.completed_files:
            return self.completed_files[reference]
        try:
            artifact = load_json_artifact(
                self.root, reference, limits=self.limits, budget=self.budget
            )
        except ImportValidationError as exc:
            if (
                exc.code == "limit_exceeded"
                or depth == 0
                and self.options.strict
                or not hasattr(exc, "artifact_sha256")
            ):
                raise
            artifact = JsonArtifact(reference, exc.artifact_sha256, exc.byte_count, None)
            self.failed({}, artifact, "root", session, exc)
            return None
        self.active_files.add(reference)
        try:
            identity = self.document(artifact.payload, artifact, "root", depth, session)
            if identity is not None:
                self.completed_files[reference] = identity
            return identity
        finally:
            self.active_files.remove(reference)

    def resolve(
        self,
        path: str,
        artifact: JsonArtifact,
        depth: int,
        session: str | None,
        notices: list[dict],
        field: str,
    ) -> str | None:
        self.budget.consume(references=1)
        try:
            relative_reference(path, self.limits)
            if Path(path).suffix.lower() != ".json":
                fail(
                    "reference", "trajectory references must be JSON files", "unsupported_reference"
                )
            reference = (Path(artifact.reference).parent / path).as_posix()
            relative_reference(reference, self.limits)
            return self.file(reference, depth + 1, session)
        except ImportValidationError as exc:
            if exc.code == "limit_exceeded":
                raise
            self.notice(notices, exc.code, artifact, field)
            return None

    def failed(
        self,
        raw: Any,
        artifact: JsonArtifact,
        locator: str,
        session: str | None,
        error: ImportValidationError,
    ) -> None:
        safe = {"schema_version": raw.get("schema_version")} if isinstance(raw, dict) else {}
        data = self.base_receipt(safe, artifact, locator, session)
        data["receipt_id"] = receipt_id(data)
        data["completeness"]["trajectory"] = "missing"
        self.notice(data["notices"], error.code, artifact, error.field, "error")
        self.receipts.append(ImportReceipt.from_dict(data, limits=self.limits))

    def document(
        self,
        raw: Any,
        artifact: JsonArtifact,
        locator: str,
        depth: int,
        inherited_session: str | None,
    ) -> str | None:
        if depth >= self.limits.max_depth:
            fail("trajectory.depth", "trajectory nesting limit exceeded", "limit_exceeded")
        self.budget.consume(trajectories=1)
        try:
            payload = validate_trajectory(raw, self.limits)
        except ImportValidationError as exc:
            if exc.code == "limit_exceeded" or self.options.strict and depth == 0:
                raise
            self.failed(raw, artifact, locator, inherited_session, exc)
            return None
        document_id = payload.get("trajectory_id")
        if document_id is not None:
            if document_id in self.document_ids:
                fail(
                    "trajectory_id", "duplicate source document identity", "duplicate_trajectory_id"
                )
            self.document_ids.add(document_id)
        receipt = self.base_receipt(payload, artifact, locator, inherited_session)
        receipt["receipt_id"] = receipt_id(receipt)
        identity = receipt["external_identity"]
        run_inputs = {
            key: identity[key] for key in ("job_id", "trial_id", "step_id", "trajectory_id")
        }
        if identity["job_id"] is None or identity["trial_id"] is None:
            run_inputs.update(
                artifact_sha256=artifact.artifact_sha256, artifact_reference=artifact.reference
            )
        run_id = external_id("run", "harbor", run_inputs)
        notices = receipt["notices"]
        children = {}
        for index, child in enumerate(payload.get("subagent_trajectories") or []):
            children[child["trajectory_id"]] = self.document(
                child,
                artifact,
                f"{locator}/subagent_trajectories/{index}",
                depth + 1,
                identity["session_id"],
            )
        root_interval = self.interval(payload.get("extra"), notices, artifact, "extra")
        trace = AgentTrace(
            name=payload["agent"]["name"],
            run_id=run_id,
            started_at=root_interval[0] if root_interval else "unknown",
            ended_at=root_interval[1] if root_interval else None,
            elapsed_ms=root_interval[2] if root_interval else None,
            metadata={
                "source": "harbor_atif",
                "external_identity": identity,
                "source_receipt_id": receipt["receipt_id"],
                "external_evidence_schema": "1.0",
                **({"synthetic": True} if self.options.synthetic_fixture else {}),
            },
        )
        source_steps = []
        reported_calls: int | None = 0
        usage_complete = True
        copied_count = 0
        for step in payload["steps"]:
            step_id = step["step_id"]
            field = f"steps[{step_id - 1}]"
            model_metrics = step.get("metrics") or {}
            observations = (step.get("observation") or {}).get("results") or []
            source_step = {
                "step_id": step_id,
                "source": step["source"],
                "timestamp": step.get("timestamp"),
                "is_copied_context": step.get("is_copied_context", False),
                "llm_call_count": step.get("llm_call_count"),
                "message": self.content(step["message"], notices, artifact, field),
                "tool_calls": [
                    {
                        "tool_call_id": call["tool_call_id"],
                        "function_name": call["function_name"],
                        "extra": self.bounded(
                            call.get("extra"), notices, artifact, field + ".tool_calls.extra"
                        ),
                        "arguments": self.arguments(
                            call["arguments"], notices, artifact, field + ".arguments"
                        ),
                    }
                    for call in step.get("tool_calls") or []
                ],
                "observations": [
                    {
                        "source_call_id": result.get("source_call_id"),
                        "subagent_trajectory_ref": [
                            self.reference_metadata(reference, notices, artifact, field)
                            for reference in result.get("subagent_trajectory_ref") or []
                        ],
                        "content": self.content(
                            result.get("content"), notices, artifact, field + ".observation"
                        ),
                        "extra": self.bounded(
                            result.get("extra"), notices, artifact, field + ".observation.extra"
                        ),
                    }
                    for result in observations
                ],
            }
            if step.get("reasoning_content") is not None:
                source_step["reasoning_content"] = self.text(
                    step["reasoning_content"], capture=self.options.capture_reasoning
                )
            retained_metrics = {
                key: value
                for key, value in model_metrics.items()
                if key not in {"prompt_token_ids", "completion_token_ids", "logprobs", "extra"}
            }
            for key in ("prompt_token_ids", "completion_token_ids", "logprobs"):
                if model_metrics.get(key) is not None:
                    values = model_metrics[key]
                    retained_metrics[key] = (
                        values
                        if self.options.capture_token_ids
                        and len(canonical_json(values).encode()) <= self.limits.max_metadata_bytes
                        else {"count": len(values), "capture": "omitted"}
                    )
            if "extra" in model_metrics:
                retained_metrics["extra"] = self.bounded(
                    model_metrics["extra"], notices, artifact, field + ".metrics.extra"
                )
            source_step["metrics"] = retained_metrics
            source_step["extra"] = self.bounded(
                step.get("extra"), notices, artifact, field + ".extra"
            )
            source_steps.append(source_step)
            if step.get("is_copied_context") is True:
                copied_count += 1
                continue
            count = step.get("llm_call_count")
            inferred_block = step["source"] == "agent" and any(
                model_metrics.get(key) is not None
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "cost_usd",
                    "prompt_token_ids",
                    "completion_token_ids",
                    "logprobs",
                )
            )
            model_block = step["source"] == "agent" and (
                count is not None and count > 0 or count is None and inferred_block
            )
            if step["source"] == "agent":
                reported_calls = (
                    None if reported_calls is None or count is None else reported_calls + count
                )
            step_interval = self.interval(step.get("extra"), notices, artifact, field + ".extra")

            def event(
                kind: str,
                name: str,
                *,
                call_id: str | None = None,
                interval: tuple | None = step_interval,
                status: str = "unknown",
                metadata: dict | None = None,
            ) -> None:
                if len(trace.events) >= self.limits.max_events_per_trace:
                    fail("events", "native event count limit exceeded", "limit_exceeded")
                event_id = external_id(
                    "event",
                    "harbor",
                    {"run_id": run_id, "step_id": step_id, "kind": kind, "tool_call_id": call_id},
                )
                trace.add_event(
                    AgentEvent(
                        event_id=event_id,
                        run_id=run_id,
                        event_type="model_call"
                        if kind == "model"
                        else "tool_call"
                        if kind == "tool"
                        else "source_step",
                        name=name,
                        started_at=interval[0] if interval else step.get("timestamp") or "unknown",
                        ended_at=interval[1] if interval else "unknown",
                        duration_ms=interval[2] if interval else 0.0,
                        model=(step.get("model_name") or payload["agent"].get("model_name"))
                        if kind == "model"
                        else None,
                        input_tokens=(model_metrics.get("prompt_tokens") or 0)
                        if kind == "model"
                        else 0,
                        output_tokens=(model_metrics.get("completion_tokens") or 0)
                        if kind == "model"
                        else 0,
                        token_provenance="external_reported"
                        if kind == "model"
                        and (
                            model_metrics.get("prompt_tokens") is not None
                            or model_metrics.get("completion_tokens") is not None
                        )
                        else "unavailable",
                        status="error" if status in {"failed", "timed_out", "cancelled"} else "ok",
                        error="External operation did not complete"
                        if status in {"failed", "timed_out", "cancelled"}
                        else None,
                        metadata={
                            "operation_kind": kind,
                            "external_evidence_schema": "1.0",
                            "source_step_id": step_id,
                            "source_step_role": step["source"],
                            "source_receipt_id": receipt["receipt_id"],
                            "timing_available": interval is not None,
                            "timing_provenance": "external_reported" if interval else "unknown",
                            "source_status_available": status
                            in {"completed", "failed", "timed_out", "cancelled"},
                            "source_execution_status": status,
                            **(metadata or {}),
                        },
                    )
                )

            if model_block:
                usage_complete &= (
                    model_metrics.get("prompt_tokens") is not None
                    and model_metrics.get("completion_tokens") is not None
                )
                event(
                    "model",
                    "model_block",
                    status=self.status(step.get("extra")),
                    metadata={
                        "llm_call_count": count,
                        "source_metrics": retained_metrics,
                        "usage_available": model_metrics.get("prompt_tokens") is not None
                        and model_metrics.get("completion_tokens") is not None,
                        "usage_provenance": "external_reported",
                        "input_tokens_available": model_metrics.get("prompt_tokens") is not None,
                        "output_tokens_available": model_metrics.get("completion_tokens")
                        is not None,
                        "cached_input_tokens": model_metrics.get("cached_tokens") or 0,
                    },
                )
            else:
                if step["source"] == "agent" and count is None:
                    usage_complete = False
                kind = (
                    "transform"
                    if step["source"] == "system"
                    and isinstance((step.get("extra") or {}).get("context_operation"), str)
                    else "agent"
                    if step["source"] == "agent"
                    else "unknown"
                )
                event(
                    kind,
                    step["source"] + "_step",
                    status=self.status(step.get("extra")),
                    metadata={"llm_call_count": count, "source_content": source_step["message"]},
                )
            for call in step.get("tool_calls") or []:
                results = [
                    item
                    for item in observations
                    if item.get("source_call_id") == call["tool_call_id"]
                ]
                if not results:
                    self.notice(
                        notices, "missing_tool_observation", artifact, field + ".tool_calls"
                    )
                call_status = self.status(call.get("extra"))
                result_statuses = [self.status(result.get("extra")) for result in results]
                if call_status == "unknown" and result_statuses:
                    call_status = next(
                        (
                            status
                            for status in result_statuses
                            if status in {"failed", "timed_out", "cancelled"}
                        ),
                        "completed"
                        if all(status == "completed" for status in result_statuses)
                        else "unknown",
                    )
                event(
                    "tool",
                    call["function_name"],
                    call_id=call["tool_call_id"],
                    interval=self.interval(
                        call.get("extra"), notices, artifact, field + ".tool_calls.extra"
                    ),
                    status=call_status,
                    metadata={
                        "tool_call_id": call["tool_call_id"],
                        "observation_available": bool(results),
                        "source_arguments": self.arguments(
                            call["arguments"], notices, artifact, field + ".tool_calls.arguments"
                        ),
                        "source_results": [
                            self.content(
                                item.get("content"),
                                notices,
                                artifact,
                                field + ".observation",
                                consume_reference=False,
                            )
                            for item in results
                        ],
                    },
                )
            for observation in observations:
                for reference in observation.get("subagent_trajectory_ref") or []:
                    self.budget.consume(references=1)
                    target = reference.get("trajectory_id")
                    resolved = children.get(target) if target is not None else None
                    if resolved is None and reference.get("trajectory_path") is not None:
                        resolved = self.resolve(
                            reference["trajectory_path"],
                            artifact,
                            depth,
                            reference.get("session_id") or identity["session_id"],
                            notices,
                            field,
                        )
                    if resolved is None:
                        self.notice(notices, "unresolved_subagent", artifact, field)
                    elif target is not None and resolved != target:
                        self.notice(notices, "reference_identity_mismatch", artifact, field)
                        resolved = None
                    receipt["relationships"].append(
                        {
                            "kind": "delegation",
                            "target_id": target
                            or resolved
                            or external_id(
                                "group",
                                "harbor",
                                {
                                    "reference_sha256": sha256(
                                        reference["trajectory_path"].encode()
                                    ).hexdigest()
                                },
                            ),
                            "basis": "external_reported",
                            "resolved": resolved is not None,
                        }
                    )
        continuation = payload.get("continued_trajectory_ref")
        if continuation is not None:
            resolved = self.resolve(
                continuation,
                artifact,
                depth,
                identity["session_id"],
                notices,
                "continued_trajectory_ref",
            )
            receipt["relationships"].append(
                {
                    "kind": "continuation",
                    "target_id": resolved
                    or external_id(
                        "group",
                        "harbor",
                        {"reference_sha256": sha256(continuation.encode()).hexdigest()},
                    ),
                    "basis": "external_reported",
                    "resolved": resolved is not None,
                }
            )
        timing_complete = bool(trace.events) and all(
            event.metadata["timing_available"] for event in trace.events
        )
        execution_status = self.status(payload.get("extra"))
        source = receipt["source"]
        trace.metadata[EXTERNAL_KEY] = {
            "schema_version": "1.0",
            "source": source,
            "receipt_id": receipt["receipt_id"],
            "runtime_ms": root_interval[2] if root_interval else None,
            "event_timing_complete": timing_complete,
            "execution_status": execution_status,
            "usage_complete": usage_complete,
            "reported_model_call_count": reported_calls,
            "comparison_eligible": False,
        }
        if not timing_complete or root_interval is None:
            self.notice(notices, "timing_unavailable", artifact)
        if execution_status == "unknown":
            self.notice(notices, "execution_status_unknown", artifact)
        receipt["outcome"]["execution_status"] = execution_status
        unresolved = (
            any(
                not item["resolved"] or item["target_id"] in self.partial_documents
                for item in receipt["relationships"]
            )
            or any(child is None or child in self.partial_documents for child in children.values())
            or any(notice["code"] == "missing_tool_observation" for notice in notices)
        )
        if unresolved:
            self.partial_documents.add(identity["trajectory_id"])
        receipt["completeness"].update(
            trajectory="partial" if unresolved else "complete",
            usage="complete" if usage_complete else "partial",
            timing="complete"
            if timing_complete and root_interval
            else "missing"
            if not any(event.metadata["timing_available"] for event in trace.events)
            else "partial",
            parentage="partial" if unresolved else "unknown",
        )
        final_metrics = dict(payload.get("final_metrics") or {})
        if "extra" in final_metrics:
            final_metrics["extra"] = self.bounded(
                final_metrics["extra"], notices, artifact, "final_metrics.extra"
            )
        metadata = {
            "document_locator": locator,
            "adapter_contract_revision": ATIF_CONTRACT_REVISION,
            "import_options": vars(self.options),
            "notes": self.text(payload["notes"], capture=self.options.capture_content)
            if payload.get("notes") is not None
            else None,
            "agent": {
                "name": payload["agent"]["name"],
                "version": payload["agent"]["version"],
                "model_name": payload["agent"].get("model_name"),
                "tool_definitions": self.bounded(
                    payload["agent"].get("tool_definitions"),
                    notices,
                    artifact,
                    "agent.tool_definitions",
                ),
                "extra": self.bounded(
                    payload["agent"].get("extra"), notices, artifact, "agent.extra"
                ),
            },
            "steps": source_steps,
            "copied_context_steps": copied_count,
            "source_final_metrics": final_metrics,
            "extra": self.bounded(payload.get("extra"), notices, artifact, "extra"),
            "scope": "one trajectory document; final aggregates may include children and are never added to step usage",
        }
        if len(canonical_json(metadata).encode()) > self.limits.max_metadata_bytes:
            self.notice(notices, "source_metadata_dropped", artifact, "source_metadata.steps")
            metadata["steps"] = {
                "capture": "omitted",
                "source_step_count": len(source_steps),
                "reason": "metadata_size_limit",
            }
        receipt["source_metadata"] = metadata
        trace_data = _native_bytes(trace)
        if len(trace_data) > self.limits.max_json_bytes:
            fail("native_trace", "native artifact exceeds configured byte limit", "limit_exceeded")
        receipt["traces"] = [
            {
                "run_id": run_id,
                "trace_file": f"traces/{run_id}.json",
                "trace_sha256": sha256(trace_data).hexdigest(),
            }
        ]
        receipt["missing_trace_reason"] = None
        self.receipts.append(ImportReceipt.from_dict(receipt, limits=self.limits))
        self.traces.append(trace)
        return identity["trajectory_id"]


def import_atif(
    path: str | Path,
    *,
    root: str | Path | None = None,
    identity: dict[str, str] | None = None,
    limits: ImportLimits = ImportLimits(),
    options: AtifOptions = AtifOptions(),
) -> AtifImportResult:
    """Import one ATIF graph; no binary, remote, verifier or task code is loaded."""
    input_path = Path(path).absolute()
    if root is None:
        source_root = input_path.parent.resolve()
        reference = input_path.name
    else:
        root_path = Path(root).absolute()
        source_root = root_path.resolve()
        try:
            reference = input_path.relative_to(root_path).as_posix()
        except ValueError as exc:
            raise ImportValidationError(
                "unsafe_path", "path", "input lies outside the source root"
            ) from exc
    context = dict(identity or {})
    if set(context) - {"job_id", "trial_id", "task_id", "task_digest", "step_id"}:
        fail("identity", "unsupported caller identity field")
    for key, value in context.items():
        label(value, "identity." + key)
    importer = _Importer(source_root, limits, options, context)
    importer.file(reference)
    return AtifImportResult(tuple(importer.traces), tuple(importer.receipts))
