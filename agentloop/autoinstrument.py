from __future__ import annotations

import importlib.util
import warnings
from dataclasses import dataclass, field
from typing import Any

# Framework integrations AgentLoop ships helpers for, mapped to the import name
# used to detect whether that framework's SDK is installed.
_DETECTABLE_INTEGRATIONS: dict[str, str] = {
    "openai": "openai",
    "langgraph": "langgraph",
    "crewai": "crewai",
}

# Integrations that are not detectable as an installed Python package because
# instrumentation happens outside this process, mapped to guidance.
_NON_PYTHON_INTEGRATIONS: dict[str, str] = {
    "vercel_ai_sdk": "JavaScript-side; call trace_from_vercel_ai_events(...) on exported telemetry",
}


@dataclass
class DetectionResult:
    """Which framework integrations are available in the current environment.

    This is a *detection* result. It reports whether each integration's SDK is
    importable in this process; it does not wrap, patch, or register anything.
    Instrument an application by applying the helpers in
    :mod:`agentloop.integrations` (or the generic decorator SDK) from that
    application's own startup.
    """

    available: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.available)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "available": self.available, "unavailable": self.unavailable}


def _module_available(module_name: str) -> bool:
    """Return True if ``module_name`` is importable, without importing it."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def detect_integrations() -> DetectionResult:
    """Detect which AgentLoop framework integrations are available here.

    Detection only checks package availability with
    :func:`importlib.util.find_spec` and never imports the framework, so it is
    cheap and side-effect free and safe to run at application startup. The result
    is deterministic for a given environment, so repeated calls return equal
    results.

    This function does not enable instrumentation. To record traces, apply the
    helpers in :mod:`agentloop.integrations` (for example
    ``instrument_openai_client``) or the generic decorator SDK from the
    application process you want to trace.
    """

    result = DetectionResult()

    for name, module_name in _DETECTABLE_INTEGRATIONS.items():
        if _module_available(module_name):
            result.available.append(name)
        else:
            result.unavailable[name] = "package not installed"

    result.unavailable.update(_NON_PYTHON_INTEGRATIONS)
    return result


# Backwards-compatible aliases. The previous names implied that calling this
# enabled instrumentation, which it never did.
InstrumentationResult = DetectionResult


def auto_instrument(**kwargs: Any) -> DetectionResult:
    """Deprecated alias for :func:`detect_integrations`.

    The old name and its ``enabled`` field wrongly implied that calling it
    activated instrumentation; it only ever detected installed frameworks. Use
    :func:`detect_integrations` instead and apply
    :mod:`agentloop.integrations` helpers from your application startup. Any
    keyword arguments are ignored and were never used.
    """

    warnings.warn(
        "auto_instrument() never enabled instrumentation; it only detects "
        "installed frameworks. It is deprecated: use detect_integrations() and "
        "apply agentloop.integrations helpers from your application startup.",
        DeprecationWarning,
        stacklevel=2,
    )
    return detect_integrations()
