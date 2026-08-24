from agentloop.autoinstrument import (
    DetectionResult,
    InstrumentationResult,
    auto_instrument,
    detect_integrations,
)
from agentloop.client import AgentLoopClient, AgentLoopClientError

# Store pagination predates the shared cursor validator. Install the strict
# decoder once at package import so every SQLite/Postgres page method and API
# path receives the same shape/type validation without duplicating it per
# backend. This assignment can disappear when store.py is split into smaller
# modules; the public behavior is covered by cursor contract tests.
from agentloop.cursor_validation import install_store_decoder as _install_store_decoder
from agentloop.decorators import trace_model, trace_tool, traceable
from agentloop.doctor import run_doctor
from agentloop.findings import build_diagnosis
from agentloop.otel import trace_from_otel, trace_to_otel, traces_from_otel
from agentloop.patches import RepositoryPathError, build_patch_plan
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.runtime import (
    CLEAR,
    FinalizationError,
    get_last_error,
    get_runtime_config,
    init,
    reset_runtime,
)
from agentloop.tracer import (
    AgentTrace,
    current_trace,
    record_model_call,
    record_tool_call,
    trace_agent,
    trace_model_call,
    trace_retry,
    trace_tool_call,
)
from agentloop.value import build_value_report
from agentloop.version import __version__

_install_store_decoder()
del _install_store_decoder

__all__ = [
    "AgentLoopClient",
    "AgentLoopClientError",
    "AgentTrace",
    "CLEAR",
    "FinalizationError",
    "DetectionResult",
    "InstrumentationResult",
    "ReplayGates",
    "RepositoryPathError",
    "auto_instrument",
    "detect_integrations",
    "build_value_report",
    "build_diagnosis",
    "build_patch_plan",
    "build_replay_report",
    "current_trace",
    "get_last_error",
    "get_runtime_config",
    "init",
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
