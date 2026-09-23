"""Portable workflow/stage metadata without runtime or framework dependencies."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields

from agentloop.operations import normalize_operation_kind, operation_kind

WORKFLOW_KEY = "agentloop.workflow"
STAGE_KEY = "agentloop.stage"
METADATA_VERSION = "1.0"
_OUTCOME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _text(value, name, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _copy_metadata(value):
    if value is None:
        return {}
    if value is not None and not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    try:
        return json.loads(json.dumps(value or {}, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("metadata must contain finite JSON values") from None


def _values(value):
    # Declarations contain only validated immutable strings/None; no deep copy.
    return {item.name: getattr(value, item.name) for item in fields(value)}


@dataclass(frozen=True)
class WorkflowInfo:
    workflow_id: str
    version: str
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    input_ref: str | None = None
    output_ref: str | None = None

    def __post_init__(self):
        for key, value in _values(self).items():
            _text(value, key, optional=key not in {"workflow_id", "version"})

    def to_dict(self):
        return {"schema_version": METADATA_VERSION, **_values(self)}


@dataclass(frozen=True)
class StageInfo:
    stage_id: str
    version: str
    kind: str | None = None
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    input_ref: str | None = None
    output_ref: str | None = None

    def __post_init__(self):
        for key, value in _values(self).items():
            _text(value, key, optional=key not in {"stage_id", "version"})


def workflow_metadata(
    info: WorkflowInfo, *, task_id=None, example_id=None, status="unknown", metadata=None
):
    """Build portable workflow metadata without importing a host framework."""
    if type(info) is not WorkflowInfo:
        raise ValueError("workflow must be WorkflowInfo")
    if status not in ("unknown", "running", "completed", "failed", "cancelled", "interrupted"):
        raise ValueError("unsupported workflow execution status")
    result = _copy_metadata(metadata)
    if WORKFLOW_KEY in result:
        raise ValueError("use WorkflowInfo instead of supplying the reserved workflow namespace")
    for key, value in (("task_id", task_id), ("example_id", example_id)):
        if value is not None:
            if type(value) is not int and (not isinstance(value, str) or not value):
                raise ValueError(f"{key} must be an integer or nonempty string")
            if key in result and (type(result[key]) is not type(value) or result[key] != value):
                raise ValueError(f"conflicting {key}")
            result[key] = value
    result[WORKFLOW_KEY] = {**info.to_dict(), "status": status, "outcome": None}
    return result


def operation_metadata(kind, *, stage=None, depends_on=None, metadata=None):
    """Annotate new operations or an existing model/tool integration's metadata.

    ``depends_on=None`` means undeclared. An explicit empty list declares a
    root operation, not general side-effect or parallel-execution safety.
    """
    _text(kind, "kind")
    if stage is not None and type(stage) is not StageInfo:
        raise ValueError("stage must be StageInfo")
    result = _copy_metadata(metadata)
    if STAGE_KEY in result:
        raise ValueError("use StageInfo instead of supplying the reserved stage namespace")
    if "operation_kind" in result and result["operation_kind"] != kind:
        raise ValueError("conflicting operation_kind")
    if depends_on is None and "depends_on" in result:
        depends_on = result["depends_on"]
    if depends_on is not None:
        if not isinstance(depends_on, (list, tuple, set, frozenset)):
            raise ValueError("depends_on must be a collection of span IDs")
        for reference in depends_on:
            _text(reference, "dependency reference")
        references = sorted(set(depends_on))
        if "depends_on" in result and (
            not isinstance(result["depends_on"], list)
            or any(not isinstance(item, str) for item in result["depends_on"])
            or sorted(set(result["depends_on"])) != references
        ):
            raise ValueError("conflicting depends_on")
        result["depends_on"] = references
    else:
        references = []
    result["operation_kind"] = kind
    result[STAGE_KEY] = {
        "schema_version": METADATA_VERSION,
        "stage": _values(stage) if stage is not None else None,
        "dependencies_declared": depends_on is not None,
        "depends_on": references,
        "outcome": None,
        "output_ref": None,
    }
    return result


def _set_outcome(value, outcome, output_ref):
    if not isinstance(outcome, str) or not _OUTCOME.fullmatch(outcome):
        raise ValueError("outcome must be a bounded label, not an output body")
    _text(output_ref, "output_ref", optional=True)
    value["outcome"] = outcome
    if output_ref is not None:
        value["output_ref"] = output_ref


def set_workflow_outcome(trace, outcome, *, output_ref=None):
    """Record a caller-defined result label/reference, independently of task quality."""
    info = trace.metadata.get(WORKFLOW_KEY)
    summary = workflow_summary(trace.metadata)
    if summary is None or summary.get("schema_status") != "supported":
        raise ValueError("trace has no supported workflow declaration")
    _set_outcome(info, outcome, output_ref)


def workflow_summary(metadata):
    """Select declared profiling fields; unknown source metadata remains untouched."""
    raw = metadata.get(WORKFLOW_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != METADATA_VERSION:
        return {
            "schema_version": raw.get("schema_version")
            if isinstance(raw.get("schema_version"), str)
            else None,
            "schema_status": "unsupported",
        }
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in ("workflow_id", "version")):
        return {"schema_version": METADATA_VERSION, "schema_status": "invalid"}
    return {
        "schema_version": METADATA_VERSION,
        "schema_status": "supported",
        "kind": "workflow",
        **{
            key: metadata[key]
            for key in ("task_id", "example_id")
            if key in metadata
            and (
                type(metadata[key]) in {str, int, bool}
                or type(metadata[key]) is float
                and math.isfinite(metadata[key])
            )
        },
        **{
            key: raw.get(key) if isinstance(raw.get(key), str) else None
            for key in (
                "workflow_id",
                "version",
                "input_schema_ref",
                "output_schema_ref",
                "input_ref",
                "output_ref",
                "status",
                "outcome",
            )
        },
        "cost_scope": "recorded_model_calls",
        # Measurement scope for model input/output token counts.
        "token_scope": "recorded_model_calls",  # nosec B105
    }


def stage_summary(event):
    metadata = getattr(event, "metadata", None)
    raw = metadata.get(STAGE_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != METADATA_VERSION:
        return {
            "schema_version": raw.get("schema_version")
            if isinstance(raw.get("schema_version"), str)
            else None,
            "schema_status": "unsupported",
            "kind": "unknown",
        }
    stage = raw.get("stage")
    stage = stage if isinstance(stage, dict) else {}
    raw_kind = stage.get("kind")
    kind = normalize_operation_kind(raw_kind) if raw_kind is not None else operation_kind(event)
    dependencies = raw.get("depends_on")
    dependencies_valid = isinstance(dependencies, list) and all(
        isinstance(value, str) and value for value in dependencies
    )
    if "depends_on" in metadata:
        parallel_dependencies = metadata["depends_on"]
        dependencies_valid = (
            dependencies_valid
            and isinstance(parallel_dependencies, list)
            and all(isinstance(value, str) and value for value in parallel_dependencies)
            and set(parallel_dependencies) == set(dependencies)
        )

    def selected_text(value):
        return value if isinstance(value, str) else None

    return {
        "schema_version": METADATA_VERSION,
        "schema_status": "supported",
        "kind": kind,
        "raw_kind": raw_kind if isinstance(raw_kind, str) else None,
        **{
            key: stage.get(key) if isinstance(stage.get(key), str) else None
            for key in (
                "stage_id",
                "version",
                "input_schema_ref",
                "output_schema_ref",
                "input_ref",
                "output_ref",
            )
        },
        "output_ref": selected_text(raw.get("output_ref"))
        or selected_text(stage.get("output_ref")),
        "outcome": selected_text(raw.get("outcome")),
        "execution_status": selected_text(raw.get("execution_status"))
        or {"ok": "completed", "error": "failed"}.get(getattr(event, "status", None), "unknown"),
        "dependency_status": "invalid"
        if raw.get("dependencies_declared") is True and not dependencies_valid
        else "declared"
        if raw.get("dependencies_declared") is True
        else "undeclared",
        "dependencies_declared": raw.get("dependencies_declared") is True and dependencies_valid,
        "depends_on": list(dependencies) if dependencies_valid else [],
    }
