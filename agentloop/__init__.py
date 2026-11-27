"""AgentLoop: trace agent execution and verify improvements.

Public names load on first use, so ``import agentloop`` and imports of
``agentloop.tracer`` and other submodules stay cheap for applications that
only record traces.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from agentloop.version import __version__

if TYPE_CHECKING:
    from agentloop.autoinstrument import (
        DetectionResult,
        InstrumentationResult,
        auto_instrument,
        detect_integrations,
        instrument,
    )
    from agentloop.client import AgentLoopClient, AgentLoopClientError
    from agentloop.content_fingerprints import content_metadata, record_content
    from agentloop.decorators import trace_model, trace_tool, traceable
    from agentloop.doctor import run_doctor
    from agentloop.findings import build_diagnosis
    from agentloop.judgment_types import (
        JudgeIdentity,
        JudgeUsage,
        JudgmentAnswer,
        JudgmentBackend,
        JudgmentEvidence,
        JudgmentRequest,
        JudgmentSpec,
        JudgmentUncertainty,
    )
    from agentloop.judgments import (
        JudgmentSession,
        LocalCallbackJudge,
        attach_judgment,
        judgment_request,
        read_judgments,
    )
    from agentloop.otel import trace_from_otel, trace_to_otel, traces_from_otel
    from agentloop.patches import RepositoryPathError, build_patch_plan
    from agentloop.replay import ReplayGates, build_replay_report
    from agentloop.retention import (
        RetentionContext,
        RetentionPolicy,
        RetentionSession,
        read_retention,
    )
    from agentloop.runtime import (
        CLEAR,
        FinalizationError,
        get_last_error,
        get_runtime_config,
        init,
        reset_runtime,
    )
    from agentloop.semantic_waste import evaluate_semantic_waste, read_semantic_waste
    from agentloop.semantic_waste_types import SemanticInvestigation
    from agentloop.structured_quality import attach_quality_report
    from agentloop.tracer import (
        AgentTrace,
        bind_trace_context,
        current_trace,
        record_model_call,
        record_tool_call,
        trace_agent,
        trace_model_call,
        trace_retry,
        trace_tool_call,
    )
    from agentloop.value import build_value_report
    from agentloop.workflows import (
        ExecutionTrace,
        StageInfo,
        WorkflowInfo,
        operation_metadata,
        record_operation,
        set_workflow_outcome,
        trace_operation,
        trace_workflow,
        workflow_metadata,
    )

# Public name -> the module that defines it.
_EXPORTS: dict[str, str] = {
    "DetectionResult": "agentloop.autoinstrument",
    "InstrumentationResult": "agentloop.autoinstrument",
    "auto_instrument": "agentloop.autoinstrument",
    "detect_integrations": "agentloop.autoinstrument",
    "instrument": "agentloop.autoinstrument",
    "AgentLoopClient": "agentloop.client",
    "AgentLoopClientError": "agentloop.client",
    "content_metadata": "agentloop.content_fingerprints",
    "record_content": "agentloop.content_fingerprints",
    "trace_model": "agentloop.decorators",
    "trace_tool": "agentloop.decorators",
    "traceable": "agentloop.decorators",
    "run_doctor": "agentloop.doctor",
    "build_diagnosis": "agentloop.findings",
    "JudgeIdentity": "agentloop.judgment_types",
    "JudgeUsage": "agentloop.judgment_types",
    "JudgmentAnswer": "agentloop.judgment_types",
    "JudgmentBackend": "agentloop.judgment_types",
    "JudgmentEvidence": "agentloop.judgment_types",
    "JudgmentRequest": "agentloop.judgment_types",
    "JudgmentSpec": "agentloop.judgment_types",
    "JudgmentUncertainty": "agentloop.judgment_types",
    "JudgmentSession": "agentloop.judgments",
    "LocalCallbackJudge": "agentloop.judgments",
    "attach_judgment": "agentloop.judgments",
    "judgment_request": "agentloop.judgments",
    "read_judgments": "agentloop.judgments",
    "trace_from_otel": "agentloop.otel",
    "trace_to_otel": "agentloop.otel",
    "traces_from_otel": "agentloop.otel",
    "RepositoryPathError": "agentloop.patches",
    "build_patch_plan": "agentloop.patches",
    "ReplayGates": "agentloop.replay",
    "build_replay_report": "agentloop.replay",
    "RetentionContext": "agentloop.retention",
    "RetentionPolicy": "agentloop.retention",
    "RetentionSession": "agentloop.retention",
    "read_retention": "agentloop.retention",
    "CLEAR": "agentloop.runtime",
    "FinalizationError": "agentloop.runtime",
    "get_last_error": "agentloop.runtime",
    "get_runtime_config": "agentloop.runtime",
    "init": "agentloop.runtime",
    "reset_runtime": "agentloop.runtime",
    "evaluate_semantic_waste": "agentloop.semantic_waste",
    "read_semantic_waste": "agentloop.semantic_waste",
    "SemanticInvestigation": "agentloop.semantic_waste_types",
    "attach_quality_report": "agentloop.structured_quality",
    "AgentTrace": "agentloop.tracer",
    "bind_trace_context": "agentloop.tracer",
    "current_trace": "agentloop.tracer",
    "record_model_call": "agentloop.tracer",
    "record_tool_call": "agentloop.tracer",
    "trace_agent": "agentloop.tracer",
    "trace_model_call": "agentloop.tracer",
    "trace_retry": "agentloop.tracer",
    "trace_tool_call": "agentloop.tracer",
    "build_value_report": "agentloop.value",
    "ExecutionTrace": "agentloop.workflows",
    "StageInfo": "agentloop.workflows",
    "WorkflowInfo": "agentloop.workflows",
    "operation_metadata": "agentloop.workflows",
    "record_operation": "agentloop.workflows",
    "set_workflow_outcome": "agentloop.workflows",
    "trace_operation": "agentloop.workflows",
    "trace_workflow": "agentloop.workflows",
    "workflow_metadata": "agentloop.workflows",
}

__all__ = [
    "SemanticInvestigation",
    "evaluate_semantic_waste",
    "read_semantic_waste",
    "JudgeIdentity",
    "JudgeUsage",
    "JudgmentAnswer",
    "JudgmentBackend",
    "JudgmentEvidence",
    "JudgmentRequest",
    "JudgmentSpec",
    "JudgmentUncertainty",
    "JudgmentSession",
    "LocalCallbackJudge",
    "attach_judgment",
    "judgment_request",
    "read_judgments",
    "AgentLoopClient",
    "AgentLoopClientError",
    "AgentTrace",
    "attach_quality_report",
    "ExecutionTrace",
    "StageInfo",
    "WorkflowInfo",
    "operation_metadata",
    "record_operation",
    "set_workflow_outcome",
    "trace_operation",
    "trace_workflow",
    "workflow_metadata",
    "CLEAR",
    "FinalizationError",
    "DetectionResult",
    "InstrumentationResult",
    "ReplayGates",
    "RetentionContext",
    "RetentionPolicy",
    "RetentionSession",
    "read_retention",
    "RepositoryPathError",
    "auto_instrument",
    "bind_trace_context",
    "detect_integrations",
    "instrument",
    "build_value_report",
    "content_metadata",
    "build_diagnosis",
    "build_patch_plan",
    "build_replay_report",
    "current_trace",
    "get_last_error",
    "get_runtime_config",
    "init",
    "record_content",
    "record_model_call",
    "record_tool_call",
    "reset_runtime",
    "run_doctor",
    "trace_agent",
    "trace_from_otel",
    "traces_from_otel",
    "trace_model",
    "trace_model_call",
    "trace_tool",
    "trace_tool_call",
    "trace_retry",
    "traceable",
    "trace_to_otel",
    "__version__",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    # Cache it so later lookups skip this function.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
