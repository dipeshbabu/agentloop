"""Bounded cohort windows built from native traces or existing aggregate artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import asdict

from agentloop.aggregate_stats import AggregateConfig, number, total
from agentloop.aggregates import TraceAggregate
from agentloop.drift_types import QualityMetric, WindowSpec, label, timestamp
from agentloop.onboarding import validate_capture
from agentloop.operations import operation_kind
from agentloop.retention import read_retention
from agentloop.rules import BUILTIN_RULES
from agentloop.workflow_types import stage_summary, workflow_summary

DRIFT_VERSION = "1.0"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def seal(value):
    result = copy.deepcopy(value)
    result["sha256"] = digest(result)
    return result


def verify(value, kind):
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != DRIFT_VERSION
        or value.get("kind") != kind
    ):
        raise ValueError(f"unsupported {kind} artifact")
    content = dict(value)
    if content.pop("sha256", None) != digest(content):
        raise ValueError(f"{kind} content hash mismatch")


def _mix():
    return {"counts": Counter(), "overflow": 0, "missing": 0, "total": 0}


def _mix_add(mix, value):
    mix["total"] += 1
    if value is None:
        mix["missing"] += 1
        return
    key = digest(value)
    if key in mix["counts"] or len(mix["counts"]) < 128:
        mix["counts"][key] += 1
    else:
        mix["overflow"] += 1


class WindowBuilder:
    def __init__(
        self,
        spec: WindowSpec,
        *,
        quality_metrics: tuple[QualityMetric, ...] = (),
        config: AggregateConfig | None = None,
        include_findings=False,
    ):
        if not isinstance(spec, WindowSpec):
            raise TypeError("spec must be WindowSpec")
        if (
            not isinstance(quality_metrics, tuple)
            or len(quality_metrics) > 16
            or any(not isinstance(metric, QualityMetric) for metric in quality_metrics)
        ):
            raise ValueError("quality_metrics must be a tuple of at most 16 declarations")
        if len({metric.name for metric in quality_metrics}) != len(quality_metrics):
            raise ValueError("quality metric names must be unique")
        if type(include_findings) is not bool:
            raise ValueError("include_findings must be boolean")
        self.spec = spec
        self.quality_metrics = tuple(sorted(quality_metrics, key=lambda metric: metric.name))
        self.config = AggregateConfig() if config is None else config
        if not isinstance(self.config, AggregateConfig):
            raise TypeError("config must be AggregateConfig")
        self.include_findings = include_findings
        self._cohorts = {}
        self._chain = digest([])
        self._sources = 0

    def _cohort(self, cohort):
        label(cohort, "cohort")
        if cohort not in self._cohorts and len(self._cohorts) >= 32:
            raise ValueError("a drift window supports at most 32 explicit cohorts")
        if cohort in self._cohorts:
            return copy.deepcopy(self._cohorts[cohort])
        return {
            "aggregate": TraceAggregate(self.config),
            "flags": {
                key: {"known": 0, "true": 0}
                for key in ("timeout", "abstention", "finding", "instrumentation")
            },
            "quality": {metric.name: {"count": 0, "sum": 0.0} for metric in self.quality_metrics},
            "mixes": {key: _mix() for key in ("model", "stage", "outcome", "decision_outcome")},
            "individual_evidence_missing_runs": 0,
            "producer_markers": {"synthetic": 0, "declared_real": 0, "unspecified": 0},
            "time_membership": {
                "native_ended_at_verified_runs": 0,
                "host_assigned_aggregate_runs": 0,
            },
        }

    def add(self, trace, *, cohort="all", timeout=None, abstention=None, quality=None):
        ended = timestamp(trace.ended_at)
        if not timestamp(self.spec.started_at) <= ended < timestamp(self.spec.ended_at):
            raise ValueError("trace completion is outside the declared window")
        for value in (timeout, abstention):
            if value is not None and type(value) is not bool:
                raise ValueError("timeout/abstention must be boolean or None")
        quality = {} if quality is None else quality
        if not isinstance(quality, dict) or not set(quality) <= {
            metric.name for metric in self.quality_metrics
        }:
            raise ValueError("quality values must match declared metrics")
        for value in quality.values():
            if value is not None and (
                type(value) not in {int, float} or not 0 <= number(value) <= 1
            ):
                raise ValueError("quality values must be in [0,1] or None")
        owned = self._cohort(cohort)
        owned["aggregate"].add(trace)
        owned["time_membership"]["native_ended_at_verified_runs"] += 1
        synthetic = trace.metadata.get("synthetic")
        owned["producer_markers"][
            "synthetic"
            if synthetic is True
            else "declared_real"
            if synthetic is False
            else "unspecified"
        ] += 1
        retained = read_retention(trace)
        complete = retained is None or retained["complete_evidence"]
        for key, value in (("timeout", timeout), ("abstention", abstention)):
            if value is not None:
                owned["flags"][key]["known"] += 1
                owned["flags"][key]["true"] += value
        for key, value in quality.items():
            if value is not None:
                owned["quality"][key]["count"] += 1
                owned["quality"][key]["sum"] = total(owned["quality"][key]["sum"], value)
        owned["flags"]["instrumentation"]["known"] += 1
        if complete:
            validation = validate_capture(trace)
            missing_codes = {
                "no_execution_spans",
                "unsupported_operation",
                "usage_not_exact",
                "model_identity_missing",
                "trace_boundary_missing",
                "span_timing_unavailable",
                "span_outside_boundary",
                "retained_evidence_incomplete",
            }
            owned["flags"]["instrumentation"]["true"] += not any(
                check["severity"] == "error" or check["code"] in missing_codes
                for check in validation["checks"]
            )
            for event in trace.events:
                if operation_kind(event) == "model":
                    _mix_add(owned["mixes"]["model"], event.model)
                stage = stage_summary(event)
                _mix_add(
                    owned["mixes"]["stage"],
                    [stage.get("stage_id"), stage.get("version")]
                    if stage and stage.get("stage_id")
                    else None,
                )
                if operation_kind(event) in {"classifier", "rule"}:
                    _mix_add(
                        owned["mixes"]["decision_outcome"], stage.get("outcome") if stage else None
                    )
            execution = workflow_summary(trace.metadata)
            _mix_add(owned["mixes"]["outcome"], execution.get("outcome") if execution else None)
            if self.include_findings and len(trace.events) <= 2000:
                report = trace.report()
                if report.get("analysis_complete"):
                    owned["flags"]["finding"]["known"] += 1
                    owned["flags"]["finding"]["true"] += bool(report["finding_candidates"])
        else:
            owned["individual_evidence_missing_runs"] += 1
        source_hash = digest(trace.to_dict())
        chain = digest([self._chain, source_hash, cohort, timeout, abstention, quality])
        # All validation/analysis/hash work precedes mutation of builder state.
        self._cohorts[cohort] = owned
        self._chain, self._sources = chain, self._sources + 1

    def add_aggregate(self, aggregate: TraceAggregate, *, cohort="all", time_assignment_ref):
        """Existing aggregates contribute original metrics, with missing per-run extras explicit."""
        if not isinstance(aggregate, TraceAggregate):
            raise TypeError("aggregate must be TraceAggregate")
        label(time_assignment_ref, "time_assignment_ref")
        owned = self._cohort(cohort)
        owned["aggregate"].merge(aggregate)
        report = aggregate.to_dict()
        owned["individual_evidence_missing_runs"] += report["counts"]["runs"]
        owned["time_membership"]["host_assigned_aggregate_runs"] += report["counts"]["runs"]
        owned["producer_markers"]["unspecified"] += report["counts"]["runs"]
        chain = digest([self._chain, report["sha256"], cohort, time_assignment_ref])
        self._cohorts[cohort] = owned
        self._chain, self._sources = chain, self._sources + 1

    def to_dict(self):
        cohorts = copy.deepcopy(self._cohorts)
        for values in cohorts.values():
            values["aggregate"] = values["aggregate"].to_dict()
            for mix in values["mixes"].values():
                mix["counts"] = dict(mix["counts"])
        return seal(
            {
                "schema_version": DRIFT_VERSION,
                "kind": "agentloop.drift_window",
                "spec": asdict(self.spec),
                "quality_metrics": [asdict(metric) for metric in self.quality_metrics],
                "settings": {
                    "include_findings": self.include_findings,
                    "rule_versions": [[rule.rule_id, rule.version] for rule in BUILTIN_RULES]
                    if self.include_findings
                    else None,
                },
                "cohorts": cohorts,
                "source_count": self._sources,
                "source_chain_sha256": self._chain,
                "scope": "Supplied disjoint records only; cohort labels and quality/timeout/abstention annotations are host observations.",
            }
        )


def validate_window(value):
    verify(value, "agentloop.drift_window")
    WindowSpec(**value["spec"])
    metrics = tuple(QualityMetric(**item) for item in value["quality_metrics"])
    if (
        type(value["source_count"]) is not int
        or value["source_count"] < 0
        or not isinstance(value["source_chain_sha256"], str)
        or len(value["source_chain_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["source_chain_sha256"])
    ):
        raise ValueError("invalid source chain")
    settings = value["settings"]
    if (
        not isinstance(settings, dict)
        or set(settings) != {"include_findings", "rule_versions"}
        or type(settings["include_findings"]) is not bool
    ):
        raise ValueError("invalid window analysis settings")
    if settings["include_findings"]:
        if not isinstance(settings["rule_versions"], list) or len(settings["rule_versions"]) > 128:
            raise ValueError("invalid finding rule versions")
        for entry in settings["rule_versions"]:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("invalid finding rule version entry")
            for field in entry:
                label(field, "finding rule version")
    elif settings["rule_versions"] is not None:
        raise ValueError("unexpected finding rule versions")
    if (
        len(metrics) > 16
        or len({metric.name for metric in metrics}) != len(metrics)
        or not isinstance(value["cohorts"], dict)
        or len(value["cohorts"]) > 32
    ):
        raise ValueError("invalid drift window dimensions")
    for cohort, data in value["cohorts"].items():
        label(cohort, "cohort")
        report = TraceAggregate.from_dict(data["aggregate"]).to_dict()
        runs = report["counts"]["runs"]
        markers = data["producer_markers"]
        if (
            set(markers) != {"synthetic", "declared_real", "unspecified"}
            or any(type(count) is not int or count < 0 for count in markers.values())
            or sum(markers.values()) != runs
        ):
            raise ValueError("invalid producer marker counts")
        membership = data["time_membership"]
        if (
            set(membership) != {"native_ended_at_verified_runs", "host_assigned_aggregate_runs"}
            or any(type(count) is not int or count < 0 for count in membership.values())
            or sum(membership.values()) != runs
        ):
            raise ValueError("invalid time membership counts")
        if (
            set(data["quality"]) != {metric.name for metric in metrics}
            or set(data["flags"]) != {"timeout", "abstention", "finding", "instrumentation"}
            or set(data["mixes"]) != {"model", "stage", "outcome", "decision_outcome"}
        ):
            raise ValueError("invalid window metric fields")
        for flag in data["flags"].values():
            if (
                any(type(flag[key]) is not int for key in ("known", "true"))
                or not 0 <= flag["true"] <= flag["known"] <= runs
            ):
                raise ValueError("invalid window flag denominator")
        for quality in data["quality"].values():
            if (
                type(quality["count"]) is not int
                or not 0 <= quality["count"] <= runs
                or not 0 <= number(quality["sum"]) <= quality["count"]
            ):
                raise ValueError("invalid window quality denominator")
        for mix in data["mixes"].values():
            counts = mix["counts"]
            if (
                not isinstance(counts, dict)
                or len(counts) > 128
                or any(
                    not isinstance(key, str)
                    or len(key) != 64
                    or any(char not in "0123456789abcdef" for char in key)
                    for key in counts
                )
            ):
                raise ValueError("invalid window mix keys")
            if (
                any(
                    type(item) is not int or item < 0
                    for item in [*counts.values(), mix["missing"], mix["overflow"], mix["total"]]
                )
                or sum(counts.values()) + mix["missing"] + mix["overflow"] != mix["total"]
            ):
                raise ValueError("invalid window mix denominator")
        if (
            type(data["individual_evidence_missing_runs"]) is not int
            or not 0 <= data["individual_evidence_missing_runs"] <= runs
        ):
            raise ValueError("invalid individual evidence count")
    return copy.deepcopy(value)
