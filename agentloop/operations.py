"""Framework-neutral operation taxonomy layered over legacy event categories."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

OPERATION_KINDS = frozenset(
    {
        "agent",
        "workflow",
        "model",
        "tool",
        "retriever",
        "memory",
        "reranker",
        "guardrail",
        "evaluator",
        "retry",
    }
)
LEGACY_OPERATION_KINDS = {"model_call": "model", "tool_call": "tool", "retry": "retry"}


def normalize_operation_kind(value: Any) -> str:
    """Unknown labels stay in source metadata and analyze as `unknown`."""
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in OPERATION_KINDS else "unknown"


def operation_kind(event: Any) -> str:
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, Mapping) and "operation_kind" in metadata:
        return normalize_operation_kind(metadata["operation_kind"])
    return LEGACY_OPERATION_KINDS.get(getattr(event, "event_type", None), "unknown")


def operation_counts(events: list[Any]) -> dict[str, int]:
    return dict(sorted(Counter(operation_kind(event) for event in events).items()))
