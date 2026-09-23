"""Frozen declarations for explicit offline decision implementation experiments."""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
from functools import cached_property
from typing import Callable

from agentloop.judgment_types import (
    JudgeIdentity,
    JudgeUsage,
    canonical,
    fingerprint,
    finite,
    is_hash,
    text,
)
from agentloop.quality import validate_quality_fixtures

SUBSTITUTION_VERSION = "1.0"
TRIAL_KEY = "agentloop.substitution_trial"
_MISSING = object()
DecisionIdentity = JudgeIdentity


@dataclass(frozen=True)
class DecisionStep:
    step_id: str
    version: str
    source_run_id: str
    source_span_id: str
    source_trace_hash: str
    synthetic: bool = False

    def __post_init__(self):
        for name in ("step_id", "version", "source_run_id", "source_span_id"):
            text(getattr(self, name), name)
        if not is_hash(self.source_trace_hash) or type(self.synthetic) is not bool:
            raise ValueError("step requires a source fingerprint and boolean synthetic marker")

    @classmethod
    def from_trace(cls, trace, span_id, *, step_id, version):
        from agentloop.tracer import AgentTrace

        owned = AgentTrace.from_dict(trace.to_dict())
        if span_id not in {item.event_id for item in owned.events}:
            raise ValueError("source span must exist in the source trace")
        return cls(
            step_id,
            version,
            owned.run_id,
            span_id,
            fingerprint(owned.to_dict()),
            owned.metadata.get("synthetic") is True,
        )


@dataclass(frozen=True, init=False)
class SubstitutionExample:
    example_id: str
    input_ref: str
    quality_ref: str
    _input_json: str = field(repr=False)
    _expected_json: str | None = field(repr=False)

    def __init__(self, example_id, inputs, *, input_ref, quality_ref, expected=_MISSING):
        for name, value in (
            ("example_id", example_id),
            ("input_ref", input_ref),
            ("quality_ref", quality_ref),
        ):
            text(value, name)
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_input_json", canonical(inputs))
        object.__setattr__(
            self, "_expected_json", None if expected is _MISSING else canonical(expected)
        )

    @property
    def inputs(self):
        return json.loads(self._input_json)

    def fixture(self, scorer):
        return {
            "schema_version": "2.0",
            "id": self.example_id,
            "input_ref": self.input_ref,
            "scorer": scorer,
            "expected_ref": self.quality_ref,
            **(
                {"expected": json.loads(self._expected_json)}
                if self._expected_json is not None
                else {}
            ),
        }

    def declaration(self):
        return {
            "example_id": self.example_id,
            "input_ref": self.input_ref,
            "input_hash": fingerprint(self.inputs),
            "quality_ref": self.quality_ref,
            "expected_hash": fingerprint(json.loads(self._expected_json))
            if self._expected_json is not None
            else None,
        }


@dataclass(frozen=True)
class DecisionRequest:
    example_id: str
    input_ref: str
    step: DecisionStep
    _input_json: str = field(repr=False)

    @cached_property
    def inputs(self):
        """Own one decoded input per invocation, isolated from every other trial."""
        return json.loads(self._input_json)


@dataclass(frozen=True)
class DecisionImplementation:
    name: str
    identity: JudgeIdentity
    invoke: Callable = field(repr=False)
    kind: str = "model"

    def __post_init__(self):
        text(self.name, "implementation name")
        if type(self.identity) is not JudgeIdentity or self.kind not in {
            "model",
            "classifier",
            "rule",
            "tool",
            "external_service",
        }:
            raise ValueError("implementation requires a versioned identity and supported kind")
        if (
            not callable(self.invoke)
            or inspect.iscoroutinefunction(self.invoke)
            or inspect.isasyncgenfunction(self.invoke)
        ):
            raise ValueError("implementation must be a trusted synchronous callable")

    def declaration(self):
        return {"name": self.name, "identity": asdict(self.identity), "kind": self.kind}


@dataclass(frozen=True, init=False)
class DecisionResult:
    status: str
    usage: JudgeUsage
    cost_ref: str | None
    _output_json: str | None = field(repr=False)

    def __init__(self, output=_MISSING, *, status="completed", usage=None, cost_ref=None):
        if status not in {"completed", "unknown"}:
            raise ValueError("result status must be completed or unknown")
        if (status == "completed") != (output is not _MISSING):
            raise ValueError(
                "completed results require an output; unknown results cannot carry one"
            )
        usage = JudgeUsage() if usage is None else usage
        if type(usage) is not JudgeUsage:
            raise ValueError("usage must be JudgeUsage")
        text(cost_ref, "cost_ref", optional=True)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "cost_ref", cost_ref)
        object.__setattr__(self, "_output_json", None if output is _MISSING else canonical(output))

    @property
    def output(self):
        if self._output_json is None:
            raise ValueError("result has no observed output")
        return json.loads(self._output_json)


@dataclass(frozen=True, init=False)
class SubstitutionProtocol:
    name: str
    version: str
    step: DecisionStep
    examples: tuple[SubstitutionExample, ...]
    repetitions: int
    seed: int
    timeout_s: float | None
    min_quality: float
    max_quality_drop: float
    synthetic: bool
    _scorer_json: str = field(repr=False)

    def __init__(
        self,
        name,
        version,
        step,
        examples,
        scorer,
        *,
        repetitions=1,
        seed=0,
        timeout_s=None,
        min_quality=1.0,
        max_quality_drop=0.0,
        synthetic=False,
    ):
        text(name, "name")
        text(version, "version")
        if (
            type(step) is not DecisionStep
            or not isinstance(examples, (tuple, list))
            or not examples
            or any(type(item) is not SubstitutionExample for item in examples)
        ):
            raise ValueError("protocol requires a source step and nonempty frozen examples")
        if len({item.example_id for item in examples}) != len(examples):
            raise ValueError("example IDs must be unique")
        if (
            type(repetitions) is not int
            or repetitions < 1
            or type(seed) is not int
            or type(synthetic) is not bool
        ):
            raise ValueError("invalid repetition, seed or synthetic declaration")
        if timeout_s is not None and (not finite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be finite and positive")
        for value in (min_quality, max_quality_drop):
            if not finite(value) or not 0 <= value <= 1:
                raise ValueError("quality thresholds must be in [0, 1]")
        if not isinstance(scorer, dict):
            raise ValueError("scorer must be a versioned structured quality configuration")
        text(scorer.get("version"), "scorer version")
        owned = json.loads(canonical(scorer))
        validate_quality_fixtures([item.fixture(owned) for item in examples])
        values = {
            "name": name,
            "version": version,
            "step": step,
            "examples": tuple(examples),
            "repetitions": repetitions,
            "seed": seed,
            "timeout_s": timeout_s,
            "min_quality": min_quality,
            "max_quality_drop": max_quality_drop,
            "synthetic": synthetic or step.synthetic,
            "_scorer_json": canonical(owned),
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @property
    def scorer(self):
        return json.loads(self._scorer_json)

    def declaration(self):
        scorer = self.scorer
        examples = []
        for item in self.examples:
            declared = item.declaration()
            if item._expected_json is None and "expected" in scorer:
                declared["expected_hash"] = fingerprint(scorer["expected"])
            examples.append(declared)
        return {
            "schema_version": SUBSTITUTION_VERSION,
            "name": self.name,
            "version": self.version,
            "step": asdict(self.step),
            "examples": examples,
            "scorer": {
                "type": scorer["type"],
                "version": scorer["version"],
                "config_hash": fingerprint(scorer),
            },
            "repetitions": self.repetitions,
            "seed": self.seed,
            "timeout_s": self.timeout_s,
            "min_quality": self.min_quality,
            "max_quality_drop": self.max_quality_drop,
            "synthetic": self.synthetic,
        }
