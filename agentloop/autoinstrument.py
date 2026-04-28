from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InstrumentationResult:
    enabled: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.enabled)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "enabled": self.enabled, "skipped": self.skipped}


def _module_exists(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


def auto_instrument(**kwargs: Any) -> InstrumentationResult:
    """Best-effort auto instrumentation for installed agent frameworks.

    The function intentionally avoids importing heavy frameworks unless they are
    already installed. It returns what can be enabled and what was skipped, so
    apps can expose this during startup without failing production traffic.
    """

    result = InstrumentationResult()

    if _module_exists("openai"):
        result.enabled.append("openai")
    else:
        result.skipped["openai"] = "package not installed"

    if _module_exists("langgraph"):
        result.enabled.append("langgraph")
    else:
        result.skipped["langgraph"] = "package not installed"

    if _module_exists("crewai"):
        result.enabled.append("crewai")
    else:
        result.skipped["crewai"] = "package not installed"

    # Vercel AI SDK is JavaScript-side; Python receives exported telemetry.
    result.skipped["vercel_ai_sdk"] = "use trace_from_vercel_ai_events(...) on exported telemetry"
    return result
