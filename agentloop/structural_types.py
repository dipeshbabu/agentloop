"""Explicit stage identities and bounded batch requests for offline experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from agentloop.budget_types import ResourceUsage
from agentloop.context_types import identifier
from agentloop.experiment_observations import record_experiment_observations
from agentloop.experiment_types import canonical, reference

STRUCTURAL_KEY = "agentloop.structural_experiment"


def publish_structural_observations(receipt, values):
    try:
        record_experiment_observations(values)
    except BaseException:
        receipt["capture_error"] = "observation_capture_failed"
        if receipt["state"] not in {"failed", "abstained"}:
            raise


@dataclass(frozen=True)
class StageReference:
    stage_id: str
    version: str
    source_ref: str
    kind: str = "transform"

    def __post_init__(self):
        identifier(self.stage_id, "stage_id")
        identifier(self.version, "stage version")
        reference(self.source_ref, "stage source")
        if self.kind not in {
            "tool",
            "transform",
            "classifier",
            "rule",
            "external_service",
            "retriever",
            "evaluator",
        }:
            raise ValueError("stage wrappers require a declared non-model operation kind")

    def declaration(self):
        return asdict(self)


@dataclass(frozen=True, init=False)
class BatchItem:
    item_id: str
    _json: str = field(repr=False)

    def __init__(self, item_id, inputs):
        identifier(item_id, "item_id")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "_json", canonical(inputs))

    @property
    def inputs(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class BatchRequest:
    batch_id: str
    items: tuple[BatchItem, ...]
    request: object = field(repr=False)

    @property
    def remaining_s(self):
        return self.request.remaining_s


@dataclass(frozen=True, init=False)
class BatchItemOutcome:
    item_id: str
    status: str
    error_code: str | None
    _json: str | None = field(repr=False)

    def __init__(self, item_id, output=None, *, status="completed", error_code=None):
        identifier(item_id, "item_id")
        if status not in {"completed", "failed", "unknown"}:
            raise ValueError("unsupported batch item status")
        if error_code is not None:
            identifier(error_code, "error_code")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "_json", canonical(output) if status == "completed" else None)

    @property
    def output(self):
        return None if self._json is None else json.loads(self._json)


@dataclass(frozen=True)
class BatchResult:
    items: tuple[BatchItemOutcome, ...]
    usage: ResourceUsage = field(default_factory=ResourceUsage)

    def __post_init__(self):
        if (
            not isinstance(self.items, (list, tuple))
            or len(self.items) > 1024
            or any(type(item) is not BatchItemOutcome for item in self.items)
            or type(self.usage) is not ResourceUsage
        ):
            raise ValueError("batch result requires bounded item outcomes and exclusive usage")
        object.__setattr__(self, "items", tuple(self.items))
