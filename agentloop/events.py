from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    return "evt_" + uuid4().hex[:16]


def new_run_id() -> str:
    return "run_" + uuid4().hex[:16]


@dataclass
class AgentEvent:
    event_id: str
    run_id: str
    event_type: str
    name: str
    started_at: str
    ended_at: str
    duration_ms: float
    parent_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    input_text: str | None = None
    output_text: str | None = None
    status: str = "ok"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "AgentEvent":
        """Build an event from a serialized dict, validating the schema contract.

        Raises :class:`agentloop.schema.TraceValidationError` (a ``ValueError``)
        for missing, wrongly typed, or out-of-range fields; unknown fields are
        ignored per the documented forward-compatibility policy.
        """

        from agentloop.schema import coerce_event_dict

        return cls(**coerce_event_dict(data, index=index))
