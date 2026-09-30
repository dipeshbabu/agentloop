"""Versioned declarations for finding-linked, caller-run offline experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from time import monotonic
from typing import Callable

from agentloop.ablation_protocol import number, timestamp
from agentloop.budget_types import Reservation, ResourceUsage
from agentloop.context_types import canonical as context_canonical
from agentloop.context_types import identifier, synchronous
from agentloop.events import utc_now_iso
from agentloop.findings import build_diagnosis
from agentloop.quality import validate_quality_fixtures
from agentloop.replay import ReplayGates
from agentloop.tracer import AgentTrace

EXPERIMENT_VERSION = "1.0"
EXPERIMENT_KEY = "agentloop.experiment"


def canonical(value):
    # Validate finite JSON/string keys, then share the existing native evidence
    # escaping convention so Unicode trace/output hashes bind without translation.
    normalized = json.loads(context_canonical(value))
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def owned(value):
    return json.loads(canonical(value))


def reference(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(name + " must be a bounded nonempty reference")


@dataclass(frozen=True)
class ExperimentBudget:
    max_invocations: int
    timeout_s: float
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self):
        number(self.max_invocations, "max_invocations", integer=True, maximum=10000)
        number(self.timeout_s, "timeout_s", maximum=86400)
        number(self.max_tokens, "max_tokens", integer=True, nullable=True)
        number(self.max_cost_usd, "max_cost_usd", nullable=True)
        if not self.max_invocations or not self.timeout_s:
            raise ValueError("experiment invocation and time limits must be positive")


@dataclass(frozen=True)
class ExperimentRunner:
    candidate_id: str
    version: str
    implementation_ref: str
    configuration_ref: str
    invoke: Callable = field(repr=False, compare=False)
    reservation: Reservation = field(default_factory=Reservation)

    def __post_init__(self):
        identifier(self.candidate_id, "candidate_id")
        identifier(self.version, "runner version")
        reference(self.implementation_ref, "implementation_ref")
        reference(self.configuration_ref, "configuration_ref")
        if not synchronous(self.invoke) or type(self.reservation) is not Reservation:
            raise ValueError("runner requires a trusted synchronous callback and reservation")

    def declaration(self):
        return owned(
            {
                "candidate_id": self.candidate_id,
                "version": self.version,
                "implementation_ref": self.implementation_ref,
                "configuration_ref": self.configuration_ref,
                "reservation": self.reservation.to_dict(),
            }
        )


@dataclass(frozen=True, init=False)
class ExperimentCase:
    case_id: str
    example_id: str
    repetition: str
    _json: str = field(repr=False)

    def __init__(
        self,
        case_id,
        baseline,
        *,
        target_finding_ids,
        inputs,
        expected,
        baseline_output,
        input_ref,
        quality_ref,
        baseline_implementation_ref,
        baseline_configuration_ref,
        example_id=None,
        repetition="0",
    ):
        identifier(case_id, "case_id")
        identifier(example_id or case_id, "example_id")
        identifier(repetition, "repetition")
        for name, value in (
            ("input_ref", input_ref),
            ("quality_ref", quality_ref),
            ("baseline_implementation_ref", baseline_implementation_ref),
            ("baseline_configuration_ref", baseline_configuration_ref),
        ):
            reference(value, name)
        if not isinstance(baseline, AgentTrace):
            raise ValueError("case requires an existing baseline trace")
        trace = AgentTrace.from_dict(baseline.to_dict())
        if trace.ended_at is None:
            raise ValueError("baseline trace must be complete before planning")
        if "output" in trace.metadata and fingerprint(trace.metadata["output"]) != fingerprint(
            baseline_output
        ):
            raise ValueError("baseline output differs from the recorded workflow output")
        diagnosis = build_diagnosis(trace)
        if (
            not isinstance(target_finding_ids, (list, tuple))
            or not target_finding_ids
            or len(target_finding_ids) > 64
        ):
            raise ValueError("explicit source finding IDs required")
        ids = sorted(set(target_finding_ids))
        available = {item["finding_id"]: item for item in diagnosis["findings"]}
        if any(identity not in available for identity in ids):
            raise ValueError("source finding must belong to this actual baseline")
        data = {
            "baseline": trace.to_dict(),
            "diagnosis": diagnosis,
            "target_finding_ids": ids,
            "inputs": inputs,
            "expected": expected,
            "baseline_output": baseline_output,
            "input_ref": input_ref,
            "quality_ref": quality_ref,
            "baseline_implementation_ref": baseline_implementation_ref,
            "baseline_configuration_ref": baseline_configuration_ref,
        }
        encoded = canonical(data)
        if len(encoded.encode("utf-8")) > 10_000_000:
            raise ValueError("case exceeds ten million bytes")
        for name, value in (
            ("case_id", case_id),
            ("example_id", example_id or case_id),
            ("repetition", repetition),
            ("_json", encoded),
        ):
            object.__setattr__(self, name, value)

    def payload(self):
        return json.loads(self._json)

    def declaration(self):
        data = self.payload()
        return {
            "case_id": self.case_id,
            "example_id": self.example_id,
            "repetition": self.repetition,
            "baseline_run_id": data["baseline"]["run_id"],
            "baseline_hash": fingerprint(data["baseline"]),
            "source_finding_ids": data["target_finding_ids"],
            "expected_effects": sorted(
                (
                    item
                    for item in data["diagnosis"]["findings"]
                    if item["finding_id"] in data["target_finding_ids"]
                ),
                key=lambda item: item["finding_id"],
            ),
            "diagnosis_hash": fingerprint(data["diagnosis"]),
            "input_ref": data["input_ref"],
            "input_hash": fingerprint(data["inputs"]),
            "quality_ref": data["quality_ref"],
            "expected_hash": fingerprint(data["expected"]),
            "baseline_output_hash": fingerprint(data["baseline_output"]),
            "baseline_implementation_ref": data["baseline_implementation_ref"],
            "baseline_configuration_ref": data["baseline_configuration_ref"],
            "synthetic": data["baseline"]["metadata"].get("synthetic") is True,
        }


@dataclass(frozen=True)
class ExperimentRequest:
    experiment_id: str
    case_id: str
    candidate_id: str
    input_ref: str
    deadline: float
    _input_json: str = field(repr=False)

    @property
    def inputs(self):
        return json.loads(self._input_json)

    @property
    def remaining_s(self):
        return max(0.0, self.deadline - monotonic())


@dataclass(frozen=True, init=False)
class ExperimentResult:
    _json: str = field(repr=False)
    usage: ResourceUsage

    def __init__(self, output, *, usage=None):
        usage = ResourceUsage() if usage is None else usage
        if type(usage) is not ResourceUsage:
            raise ValueError("experiment usage must be ResourceUsage")
        encoded = canonical(output)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("experiment output exceeds one million bytes")
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "usage", usage)

    @property
    def output(self):
        return json.loads(self._json)


@dataclass(frozen=True, init=False)
class ExperimentPlan:
    _json: str = field(repr=False)
    experiment_id: str

    def __init__(
        self,
        name,
        *,
        cases,
        runners,
        intervention_type,
        intervention_version,
        scorer,
        gate_version,
        gates,
        budget,
        permission_ref,
        frozen_at=None,
        synthetic=False,
    ):
        identifier(name, "experiment name")
        identifier(intervention_type, "intervention_type")
        identifier(intervention_version, "intervention_version")
        identifier(gate_version, "gate_version")
        reference(permission_ref, "permission_ref")
        if (
            not isinstance(cases, (tuple, list))
            or not 1 <= len(cases) <= 1000
            or any(type(case) is not ExperimentCase for case in cases)
        ):
            raise ValueError("experiment requires between one and one thousand frozen cases")
        if (
            not isinstance(runners, (tuple, list))
            or not 1 <= len(runners) <= 16
            or any(type(runner) is not ExperimentRunner for runner in runners)
        ):
            raise ValueError("experiment requires bounded explicit runner bindings")
        if (
            len({case.case_id for case in cases}) != len(cases)
            or len({(case.example_id, case.repetition) for case in cases}) != len(cases)
            or len({case.payload()["baseline"]["run_id"] for case in cases}) != len(cases)
            or len({runner.candidate_id for runner in runners}) != len(runners)
        ):
            raise ValueError("case, baseline-run, pairing and candidate identities must be unique")
        if (
            type(budget) is not ExperimentBudget
            or type(gates) is not ReplayGates
            or type(synthetic) is not bool
        ):
            raise ValueError("typed budget/gates and explicit synthetic marker required")
        if gates.min_quality_score is None:
            raise ValueError("experiment gates require independent quality")
        for key, value in asdict(gates).items():
            if key.startswith("require_"):
                if type(value) is not bool:
                    raise ValueError("gate flags must be booleans")
            else:
                number(value, key, maximum=1 if key == "min_quality_score" else None)
        scorer = owned(scorer)
        if not isinstance(scorer, dict) or scorer.get("type") == "custom":
            raise ValueError(
                "experiment scorer must be deterministic; executable imports are unsupported"
            )
        identifier(scorer.get("version"), "scorer version")
        validate_quality_fixtures(
            [
                {
                    "schema_version": "2.0",
                    "id": case.case_id,
                    "expected": case.payload()["expected"],
                    "scorer": scorer,
                }
                for case in cases
            ]
        )
        for runner in runners:
            if (
                budget.max_tokens is not None
                and not runner.reservation.tokens_known
                or budget.max_cost_usd is not None
                and not runner.reservation.cost_known
            ):
                raise ValueError("resource limits require declared conservative reservation bounds")
        when = frozen_at or utc_now_iso()
        timestamp(when)
        if any(
            timestamp(case.payload()["baseline"]["ended_at"]) > timestamp(when) for case in cases
        ):
            raise ValueError("baseline outcomes must precede the frozen candidate plan")
        data = {
            "schema_version": EXPERIMENT_VERSION,
            "name": name,
            "frozen_at": when,
            "permission_ref": permission_ref,
            "intervention": {"type": intervention_type, "version": intervention_version},
            "cases": [case.declaration() for case in cases],
            "runners": [runner.declaration() for runner in runners],
            "scorer": scorer,
            "scorer_hash": fingerprint(scorer),
            "gate_version": gate_version,
            "gates": asdict(gates),
            "budget": asdict(budget),
            "budget_scope": "new_candidate_attempt_admissions_conservative_reservations_no_refunds",
            "pairing_keys": ["experiment_id", "case_id"],
            "artifact_layout": {
                "baselines": "baselines/<case-sha256>.json",
                "attempts": "attempts/<slot-sha256>/",
                "reports": "reports/<snapshot-sha256>/",
            },
            "synthetic": synthetic or any(case.declaration()["synthetic"] for case in cases),
        }
        encoded = canonical(data)
        if len(encoded.encode("utf-8")) > 20_000_000:
            raise ValueError("experiment plan exceeds twenty million bytes")
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "experiment_id", "experiment_" + fingerprint(data))

    def to_dict(self):
        return json.loads(self._json)
