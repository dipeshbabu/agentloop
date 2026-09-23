"""Frozen native fixtures and independent labels for finding evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agentloop.judgment_types import canonical, fingerprint, text
from agentloop.tracer import AgentTrace

FINDING_BENCHMARK_VERSION = "1.0"
LABELS = {"opportunity", "no_opportunity", "unknown", "ambiguous"}


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ValueError(f"invalid {name} fields")


@dataclass(frozen=True, init=False)
class FindingBenchmark:
    """Own a validated JSON snapshot; returned dictionaries cannot mutate the plan.

    Labels enumerate justified findings by exact family and affected-span set.
    They are never passed to detectors. Group declarations prevent splitting one
    workload family across development and evaluation, not all possible leakage.
    """

    _json: str

    def __init__(self, value):
        owned = json.loads(canonical(value))
        _keys(owned, "schema_version name version provenance_ref synthetic policy cases", "corpus")
        if owned["schema_version"] != FINDING_BENCHMARK_VERSION:
            raise ValueError("unsupported finding benchmark version")
        for key in ("name", "version", "provenance_ref"):
            text(owned[key], key)
        if type(owned["synthetic"]) is not bool:
            raise ValueError("synthetic must be boolean")
        policy = owned["policy"]
        _keys(policy, "id version ref", "frozen policy")
        for key, val in policy.items():
            text(val, key)
        cases = owned["cases"]
        if not isinstance(cases, list) or not 1 <= len(cases) <= 10000:
            raise ValueError("corpus requires 1..10000 cases")
        seen, groups, traces = set(), {}, {}
        span_count = 0
        for case in cases:
            _keys(
                case,
                "id workload_ref group split scenario rule_id label label_ref label_version "
                "rationale opportunities required_evidence trace",
                "case",
            )
            for key in (
                "id",
                "workload_ref",
                "group",
                "scenario",
                "rule_id",
                "label_ref",
                "label_version",
                "rationale",
            ):
                text(case[key], key)
            if case["id"] in seen:
                raise ValueError("case IDs must be unique")
            seen.add(case["id"])
            split = case["split"]
            if split not in {"development", "evaluation"} or case["label"] not in LABELS:
                raise ValueError("invalid split or label")
            if groups.setdefault(case["group"], split) != split:
                raise ValueError("workload groups must not cross splits")
            trace = AgentTrace.from_dict(case["trace"])
            span_count += len(trace.events)
            if span_count > 100000:
                raise ValueError("corpus exceeds 100000 total spans")
            ids = {event.event_id for event in trace.events}
            if len(ids) != len(trace.events) or len(ids) > 10000:
                raise ValueError("trace requires unique span IDs and at most 10000 spans")
            if traces.setdefault(fingerprint(case["trace"]), split) != split:
                raise ValueError("duplicate traces must not cross splits")
            opportunities = case["opportunities"]
            if not isinstance(opportunities, list):
                raise ValueError("opportunities must be a list")
            signatures = set()
            for opportunity in opportunities:
                _keys(opportunity, "family spans", "opportunity")
                text(opportunity["family"], "family")
                spans = opportunity["spans"]
                if (
                    not isinstance(spans, list)
                    or not spans
                    or any(not isinstance(span, str) or span not in ids for span in spans)
                    or len(set(spans)) != len(spans)
                ):
                    raise ValueError("opportunities require unique existing spans")
                signature = (opportunity["family"], tuple(sorted(spans)))
                if signature in signatures:
                    raise ValueError("duplicate opportunity")
                signatures.add(signature)
            if bool(opportunities) != (case["label"] == "opportunity"):
                raise ValueError("only opportunity labels enumerate justified findings")
            evidence = case["required_evidence"]
            if (
                not isinstance(evidence, list)
                or any(
                    not isinstance(item, str)
                    or item not in {"timing", "tokens", "cost", "semantic"}
                    for item in evidence
                )
                or len(set(evidence)) != len(evidence)
            ):
                raise ValueError("unsupported evidence requirement")
        object.__setattr__(self, "_json", canonical(owned))

    def to_dict(self):
        return json.loads(self._json)

    @property
    def corpus_hash(self):
        return fingerprint(self.to_dict())

    def cases(self, split):
        if split not in {"development", "evaluation"}:
            raise ValueError("select development or evaluation explicitly")
        cases = [item for item in self.to_dict()["cases"] if item["split"] == split]
        if not cases:
            raise ValueError("selected split is empty")
        return cases
