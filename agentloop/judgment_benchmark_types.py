"""Frozen examples and execution declarations for backend comparisons."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from agentloop.judgment_types import JudgmentRequest, canonical, fingerprint, finite, text

BENCHMARK_VERSION = "1.0"


@dataclass(frozen=True)
class JudgmentExample:
    example_id: str
    request: JudgmentRequest
    expected: Any = None
    label_status: str = "unlabelled"
    label_ref: str | None = None
    label_version: str | None = None

    def __post_init__(self):
        text(self.example_id, "example_id")
        if type(self.request) is not JudgmentRequest:
            raise ValueError("example requires JudgmentRequest")
        if self.label_status not in {"independent", "unlabelled"}:
            raise ValueError("labels must be independently sourced or unavailable")
        if self.label_status == "independent":
            text(self.label_ref, "label_ref")
            text(self.label_version, "label_version")
            valid = (
                type(self.expected) is bool
                if self.request.spec.kind == "probability"
                else self.request.spec.accepts(self.expected)
            )
            if not valid:
                raise ValueError("reference label violates the example's type/domain")
        elif (
            self.expected is not None
            or self.label_ref is not None
            or self.label_version is not None
        ):
            raise ValueError("unlabelled examples cannot carry an expected answer")

    def to_dict(self):
        return json.loads(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value):
        owned = dict(value)
        owned["request"] = JudgmentRequest.from_dict(owned["request"])
        return cls(**owned)


@dataclass(frozen=True)
class JudgmentBenchmark:
    name: str
    version: str
    examples: tuple[JudgmentExample, ...]
    repetitions: int = 1
    seed: int = 0
    timeout_s: float | None = None
    synthetic: bool = False

    def __post_init__(self):
        text(self.name, "name")
        text(self.version, "version")
        if (
            not isinstance(self.examples, (list, tuple))
            or not self.examples
            or any(type(item) is not JudgmentExample for item in self.examples)
        ):
            raise ValueError("benchmark requires a nonempty sequence of JudgmentExample")
        object.__setattr__(self, "examples", tuple(self.examples))
        if len({item.example_id for item in self.examples}) != len(self.examples):
            raise ValueError("example IDs must be unique")
        if type(self.repetitions) is not int or self.repetitions < 1:
            raise ValueError("repetitions must be a positive integer")
        if type(self.seed) is not int or type(self.synthetic) is not bool:
            raise ValueError("seed and synthetic require an integer and boolean")
        if self.timeout_s is not None and (not finite(self.timeout_s) or self.timeout_s <= 0):
            raise ValueError("timeout_s must be finite and positive")

    def to_dict(self):
        return {"schema_version": BENCHMARK_VERSION, **json.loads(canonical(asdict(self)))}

    @property
    def protocol_hash(self):
        return fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        owned = dict(value)
        if owned.pop("schema_version") != BENCHMARK_VERSION:
            raise ValueError("unsupported judgment benchmark version")
        owned["examples"] = tuple(JudgmentExample.from_dict(item) for item in owned["examples"])
        return cls(**owned)
