"""Framework-neutral workflow/stage tracing using the existing trace lifecycle."""

from __future__ import annotations

import math
import time
from asyncio import CancelledError
from contextlib import contextmanager

from agentloop.events import AgentEvent, new_event_id, utc_now_iso
from agentloop.operations import normalize_operation_kind
from agentloop.tokens import EXACT_PROVENANCE, validate_provenance
from agentloop.tracer import (
    AgentTrace,
    _trace_execution,
    bind_trace_context,
    current_event_id,
    current_trace,
)
from agentloop.workflow_types import (
    STAGE_KEY,
    WORKFLOW_KEY,
    _set_outcome,
    _text,
)
from agentloop.workflow_types import (
    StageInfo as StageInfo,
)
from agentloop.workflow_types import (
    WorkflowInfo as WorkflowInfo,
)
from agentloop.workflow_types import (
    operation_metadata as operation_metadata,
)
from agentloop.workflow_types import (
    set_workflow_outcome as set_workflow_outcome,
)
from agentloop.workflow_types import (
    stage_summary as stage_summary,
)
from agentloop.workflow_types import (
    workflow_metadata as workflow_metadata,
)
from agentloop.workflow_types import (
    workflow_summary as workflow_summary,
)

ExecutionTrace = AgentTrace


def _status(error):
    if isinstance(error, CancelledError):
        return "cancelled"
    if isinstance(error, (GeneratorExit, KeyboardInterrupt)):
        return "interrupted"
    return "failed"


@contextmanager
def trace_workflow(name, *, workflow, task_id=None, example_id=None, metadata=None):
    """Open the same trace lifecycle/store path used by trace_agent."""
    _text(name, "name")
    owned = workflow_metadata(workflow, task_id=task_id, example_id=example_id, metadata=metadata)
    with _trace_execution(name, owned) as trace:
        info = trace.metadata[WORKFLOW_KEY]
        info["status"] = "running"
        try:
            yield trace
        except BaseException as exc:
            info["status"] = _status(exc)
            raise
        else:
            info["status"] = "completed"


class OperationSpan:
    """The current span's stable identity and optional payload-free result."""

    def __init__(self, event_id):
        self._event_id, self._result, self._finished = event_id, {}, False

    @property
    def event_id(self):
        return self._event_id

    def set_outcome(self, outcome, *, output_ref=None):
        if self._finished:
            raise RuntimeError("operation span has already finished")
        _set_outcome(self._result, outcome, output_ref)


def _record(
    name,
    kind,
    *,
    duration_ms,
    started_at,
    ended_at,
    metadata,
    model=None,
    input_tokens=None,
    output_tokens=None,
    token_provenance=None,
    status="ok",
    error=None,
    trace=None,
    parent_id=None,
    event_id=None,
):
    target = trace if trace is not None else current_trace()
    if target is None:
        raise RuntimeError("No active AgentLoop trace")
    parent = (
        parent_id if parent_id is not None else None if trace is not None else current_event_id()
    )
    category, provenance = _usage(kind, input_tokens, output_tokens, token_provenance)
    try:
        valid_duration = (
            type(duration_ms) in {int, float} and math.isfinite(duration_ms) and duration_ms >= 0
        )
    except OverflowError:
        valid_duration = False
    if not valid_duration:
        raise ValueError("duration_ms must be finite and nonnegative")
    _text(name, "name")
    _text(model, "model", optional=True)
    _text(event_id, "event_id", optional=True)
    _text(parent, "parent_id", optional=True)
    event = AgentEvent(
        event_id=event_id or new_event_id(),
        run_id=target.run_id,
        event_type=category,
        name=name,
        started_at=started_at,
        ended_at=ended_at or utc_now_iso(),
        duration_ms=duration_ms,
        parent_id=parent,
        model=model,
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        token_provenance=provenance,
        status=status,
        error=error,
        metadata=metadata,
    )
    # Validate the completed event with the same native schema as imports.
    target.add_event(AgentEvent.from_dict(event.to_dict()))


def _usage(kind, input_tokens, output_tokens, token_provenance):
    category = {"model": "model_call", "tool": "tool_call", "retry": "retry"}.get(
        normalize_operation_kind(kind), "operation"
    )
    for value in (input_tokens, output_tokens):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("token counts must be nonnegative integers or unknown")
    if category != "model_call":
        if input_tokens is not None or output_tokens is not None or token_provenance is not None:
            raise ValueError(
                "token usage requires kind='model'; a stage can declare a different logical kind"
            )
        return category, None
    known = input_tokens is not None and output_tokens is not None
    provenance = (
        ("user_supplied" if known else "unavailable")
        if token_provenance is None
        else token_provenance
    )
    validate_provenance(provenance)
    if not known and provenance in EXACT_PROVENANCE:
        raise ValueError("exact usage requires both input and output token counts")
    return category, provenance


def record_operation(
    name,
    *,
    kind,
    duration_ms,
    started_at,
    ended_at=None,
    stage=None,
    depends_on=None,
    outcome=None,
    output_ref=None,
    metadata=None,
    model=None,
    input_tokens=None,
    output_tokens=None,
    token_provenance=None,
    status="ok",
    error=None,
    trace=None,
    parent_id=None,
    event_id=None,
):
    """Record completed work, including callbacks with an explicitly captured trace."""
    owned = operation_metadata(kind, stage=stage, depends_on=depends_on, metadata=metadata)
    if outcome is not None:
        _set_outcome(owned[STAGE_KEY], outcome, output_ref)
    elif output_ref is not None:
        _text(output_ref, "output_ref")
        owned[STAGE_KEY]["output_ref"] = output_ref
    _record(
        name,
        kind,
        duration_ms=duration_ms,
        started_at=started_at,
        ended_at=ended_at,
        metadata=owned,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_provenance=token_provenance,
        status=status,
        error=error,
        trace=trace,
        parent_id=parent_id,
        event_id=event_id,
    )


@contextmanager
def trace_operation(
    name,
    *,
    kind,
    stage=None,
    depends_on=None,
    metadata=None,
    model=None,
    input_tokens=None,
    output_tokens=None,
    token_provenance=None,
    capture_error_detail=False,
):
    """Measure one operation; ordinary bodies/errors are not captured by default."""
    target = current_trace()
    if target is None:
        raise RuntimeError("No active AgentLoop trace")
    if type(capture_error_detail) is not bool:
        raise ValueError("capture_error_detail must be a boolean")
    _text(name, "name")
    _text(model, "model", optional=True)
    _usage(kind, input_tokens, output_tokens, token_provenance)
    owned = operation_metadata(kind, stage=stage, depends_on=depends_on, metadata=metadata)
    span = OperationSpan(new_event_id())
    parent, started = current_event_id(), utc_now_iso()
    began = time.perf_counter()
    status, detail, original = "ok", None, None
    try:
        with bind_trace_context(target, span.event_id):
            yield span
    except BaseException as exc:
        original, status = exc, "error"
        owned["error_type"] = type(exc).__name__
        owned[STAGE_KEY]["execution_status"] = _status(exc)
        detail = str(exc) if capture_error_detail else None
        raise
    finally:
        span._finished = True
        owned[STAGE_KEY].update(span._result)
        owned[STAGE_KEY].setdefault("execution_status", "completed")
        try:
            _record(
                name,
                kind,
                duration_ms=(time.perf_counter() - began) * 1000,
                started_at=started,
                ended_at=utc_now_iso(),
                metadata=owned,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                token_provenance=token_provenance,
                status=status,
                error=detail,
                trace=target,
                parent_id=parent,
                event_id=span.event_id,
            )
        except BaseException:
            if original is None:
                raise
