"""Deterministic investigation ordering; scores never authorize a change."""

from __future__ import annotations

import math
import re
from collections import Counter
from copy import deepcopy
from enum import Enum

from agentloop.savings import SavingsItem, select_compatible

RANKING_KEY = "agentloop.ranking_inputs"
RANKING_VERSION = "1.0"
SORT_FIELDS = (
    "priority",
    "impact",
    "frequency",
    "latency",
    "cost",
    "tokens",
    "confidence",
    "evidence",
    "quality_risk",
    "reversible",
    "validation_effort",
    "validation_cost",
)


class RankingSort(str, Enum):
    PRIORITY = "priority"
    IMPACT = "impact"
    FREQUENCY = "frequency"
    LATENCY = "latency"
    COST = "cost"
    TOKENS = "tokens"
    CONFIDENCE = "confidence"
    EVIDENCE = "evidence"
    QUALITY_RISK = "quality_risk"
    REVERSIBLE = "reversible"
    VALIDATION_EFFORT = "validation_effort"
    VALIDATION_COST = "validation_cost"


REQUIRED_INPUTS = ("quality_risk", "reversible", "validation_effort_minutes", "validation_cost_usd")
_CONFIDENCE = {"high": 3, "medium": 2, "low": 1}
_EVIDENCE = {
    "observed": 3,
    "declared": 3,
    "deterministic_inference": 2,
    "inferred": 1,
    "semantic_judgment": 1,
}
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
ORDER = (
    "ready_to_test",
    "confidence",
    "evidence",
    "quality_risk",
    "frequency",
    "impact",
    "validation_effort",
    "validation_cost",
    "latency",
    "cost",
    "tokens",
    "identity",
)


def requirements(values):
    if (
        not isinstance(values, (list, tuple))
        or len(values) > 32
        or any(not isinstance(value, str) or not _KEY.fullmatch(value) for value in values)
    ):
        raise ValueError("ranking requirements must be bounded input identifiers")
    result = tuple(sorted(set(REQUIRED_INPUTS) | set(values)))
    if len(result) > 32:
        raise ValueError("ranking requirements are limited to 32 including standard inputs")
    return result


def _number(value, *, integer=False):
    try:
        return (
            (type(value) is int if integer else type(value) in {int, float})
            and math.isfinite(value)
            and value >= 0
        )
    except (OverflowError, TypeError):
        return False


def _input(inputs, name, validate, kind=None):
    item = inputs.get(name)
    if not isinstance(item, dict) or set(item) != {"value", "kind", "source_ref"}:
        return None
    if (
        not isinstance(item["kind"], str)
        or item["kind"] not in {"observed", "declared", "estimated"}
        or (kind is not None and item["kind"] != kind)
        or not isinstance(item["source_ref"], str)
        or not item["source_ref"].strip()
        or len(item["source_ref"]) > 512
    ):
        return None
    return deepcopy(item) if validate(item["value"]) else None


def _component(value, kind, source):
    return {"value": value, "kind": kind, "source_ref": source}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sum(values):
    try:
        result = math.fsum(values)
        return result if _number(result) else None
    except (OverflowError, ValueError):
        return None


def _units(components):
    for name, unit in {
        "impact": "ms_of_recorded_span_work",
        "frequency": "included_runs",
        "latency": "ms",
        "cost": "USD",
        "tokens": "tokens",
        "validation_effort": "minutes",
        "validation_cost": "USD",
    }.items():
        components[name]["unit"] = unit


def finding_ranking(finding, *, inputs=None, frequency=1):
    """Use explicit annotations plus preserved canonical evidence/estimates.

    Span work is cumulative observed duration, not elapsed savings. Confidence
    categories and evidence tiers are ordinal labels, not probabilities.
    """
    inputs = inputs if isinstance(inputs, dict) else {}
    retained, invalid = {}, []
    raw_spans = finding.get("affected_spans", ())
    spans = (
        {span for span in raw_spans if isinstance(span, str)}
        if isinstance(raw_spans, (list, tuple))
        else set()
    )
    evidence = finding.get("evidence", ())
    evidence = evidence if isinstance(evidence, (list, tuple)) else ()
    observed = {}
    conflicts = False
    for row in evidence:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("span_id"), str)
            or row.get("span_id") not in spans
            or not _number(row.get("duration_ms"))
        ):
            continue
        identity = row["span_id"]
        if identity in observed and observed[identity] != row["duration_ms"]:
            conflicts = True
        observed[identity] = row["duration_ms"]
    complete = bool(spans) and set(observed) == spans and not conflicts
    confidence = finding.get("confidence", "unknown")
    confidence = confidence if isinstance(confidence, str) else "unknown"
    level = finding.get("evidence_level", "unknown")
    level = level if isinstance(level, str) else "unknown"
    estimate = finding.get("estimate")
    estimate = estimate if isinstance(estimate, dict) else {}
    savings = finding.get("savings")
    savings = savings if isinstance(savings, dict) else {}
    unmodeled = estimate.get("unmodeled_metrics", ())
    if not isinstance(unmodeled, (list, tuple)):
        unmodeled = ("latency", "cost")
    source = (
        "estimator:"
        + str(estimate.get("estimator_id", "unavailable"))
        + ":"
        + str(estimate.get("estimator_version", "unknown"))
    )
    latency = savings.get("estimated_latency_savings_ms")
    cost = savings.get("estimated_cost_savings_usd")
    cost_status = _mapping(estimate.get("inputs")).get(
        "cost_status", _mapping(finding.get("metadata")).get("cost_status")
    )
    modeled = all(
        isinstance(estimate.get(key), str) and estimate[key]
        for key in ("estimator_id", "estimator_version")
    )
    latency = latency if modeled and "latency" not in unmodeled and _number(latency) else None
    cost = (
        cost
        if modeled
        and "cost" not in unmodeled
        and isinstance(cost_status, str)
        and cost_status in {"complete", "empty"}
        and _number(cost)
        else None
    )
    components = {
        "impact": _component(
            _sum(observed.values()) if complete else None,
            "observed",
            "recorded_span_durations_nonadditive",
        ),
        "frequency": _component(
            frequency if _number(frequency, integer=True) else None,
            "observed",
            "included_distinct_runs",
        ),
        "confidence": _component(
            confidence if confidence in _CONFIDENCE else None,
            "declared",
            "rule:"
            + str(finding.get("rule_id", "unknown"))
            + ":"
            + str(finding.get("rule_version", "unknown")),
        ),
        "evidence": _component(
            level if complete and level in _EVIDENCE else None,
            "declared",
            "rule_evidence_level_and_span_coverage",
        ),
        "latency": _component(latency, "estimated", source),
        "cost": _component(cost, "estimated", source),
        "tokens": _component(None, "estimated", "unavailable"),
    }
    for name, key, validate, kind in (
        (
            "quality_risk",
            "quality_risk",
            lambda value: value in {"low", "medium", "high"} if isinstance(value, str) else False,
            "declared",
        ),
        ("reversible", "reversible", lambda value: type(value) is bool, "declared"),
        (
            "evidence_level",
            "evidence",
            lambda value: isinstance(value, str) and value in _EVIDENCE,
            "declared",
        ),
        ("validation_effort_minutes", "validation_effort", _number, "declared"),
        ("validation_cost_usd", "validation_cost", _number, "declared"),
        ("estimated_cost_savings_usd", "cost", _number, "estimated"),
        ("estimated_latency_savings_ms", "latency", _number, "estimated"),
        (
            "estimated_token_savings",
            "tokens",
            lambda value: _number(value, integer=True),
            "estimated",
        ),
    ):
        supplied = _input(inputs, name, validate, kind)
        if supplied is not None:
            components[key] = supplied
            retained[name] = deepcopy(supplied)
        elif key not in components:
            components[key] = _component(None, kind, "unavailable")
        if name in inputs and supplied is None:
            invalid.append(name)
    try:
        needed = requirements(finding.get("ranking_requirements", ()))
    except ValueError:
        needed = requirements(())
        invalid.append("ranking_requirements")
    aliases = {
        "validation_effort_minutes": "validation_effort",
        "validation_cost_usd": "validation_cost",
    }
    missing = []
    for name in needed:
        component = components.get(aliases.get(name, name))
        if component is None:
            component = _input(
                inputs,
                name,
                lambda value: (
                    type(value) is bool
                    or _number(value)
                    or (isinstance(value, str) and 0 < len(value.strip()) <= 256)
                ),
            )
            components[name] = component or _component(None, "declared", "unavailable")
            if component is not None:
                retained[name] = deepcopy(component)
        if component is None or component["value"] is None:
            missing.append(name)
    reasons = ["missing_input:" + name for name in sorted(missing)]
    reasons.extend("invalid_input:" + name for name in sorted(invalid))
    if cost is None and components["cost"]["value"] is None:
        reasons.append("cost_opportunity_unavailable")
    if not complete or components["impact"]["value"] is None:
        reasons.append("span_evidence_incomplete")
    if _CONFIDENCE.get(confidence, 0) < 2:
        reasons.append("confidence_weak_or_unknown")
    if not all(
        isinstance(finding.get(key), str) and finding[key] for key in ("rule_id", "rule_version")
    ):
        reasons.append("confidence_provenance_missing")
    if _EVIDENCE.get(components["evidence"]["value"], 0) < 2:
        reasons.append("evidence_weak_or_unknown")
    if components["quality_risk"]["value"] == "high":
        reasons.append("high_quality_risk_requires_review")
    if components["reversible"]["value"] is False:
        reasons.append("irreversible_change_requires_review")
    if all(components[key]["value"] in {None, 0} for key in ("latency", "cost", "tokens")):
        reasons.append("positive_opportunity_unavailable")
    _units(components)
    return {
        "schema_version": RANKING_VERSION,
        "method": "explicit_lexicographic",
        "status": "ready_to_test" if not reasons else "needs_evidence_or_review",
        "components": components,
        "required_inputs": list(needed),
        "reasons": sorted(set(reasons)),
        "order": list(ORDER),
        "estimate_provenance": deepcopy(estimate) or None,
        "source_inputs": retained,
        "invalid_inputs": sorted(invalid),
        "automatic_application_allowed": False,
    }


def _identity(item):
    return item.get("finding_id", item.get("queue_id", ""))


def _priority(item):
    rank = item["ranking"]
    value = {key: part["value"] for key, part in rank["components"].items()}
    return (
        rank["status"] != "ready_to_test",
        -_CONFIDENCE.get(value["confidence"], 0),
        -_EVIDENCE.get(value["evidence"], 0),
        {"low": 0, "medium": 1, "high": 2}.get(value["quality_risk"], 3),
        -(value["frequency"] or 0),
        -(value["impact"] or 0),
        math.inf if value["validation_effort"] is None else value["validation_effort"],
        math.inf if value["validation_cost"] is None else value["validation_cost"],
        -(value["latency"] or 0),
        -(value["cost"] or 0),
        -(value["tokens"] or 0),
        _identity(item),
    )


def sort_ranked(items, sort_by="priority"):
    if sort_by not in SORT_FIELDS:
        raise ValueError("unsupported finding ranking dimension")
    if sort_by == "priority":
        return sorted(items, key=_priority)

    def key(item):
        value = item["ranking"]["components"][sort_by]["value"]
        if sort_by == "confidence":
            value = _CONFIDENCE.get(value)
        elif sort_by == "evidence":
            value = _EVIDENCE.get(value)
        elif sort_by == "quality_risk":
            value = {"low": 0, "medium": 1, "high": 2}.get(value)
        return (
            value is None,
            0
            if value is None
            else value
            if sort_by in {"validation_effort", "validation_cost", "quality_risk"}
            else -value,
            _identity(item),
        )

    return sorted(items, key=key)


def rank_findings(findings, *, inputs=None, sort_by="priority"):
    inputs = inputs if isinstance(inputs, dict) else {}
    result = deepcopy(findings)
    for finding in result:
        declared = _mapping(inputs.get(finding.get("rule_id")))
        if finding["finding_id"] in inputs:
            selected = inputs[finding["finding_id"]]
            declared = {**declared, **selected} if isinstance(selected, dict) else {}
        finding["ranking"] = finding_ranking(finding, inputs=declared)
    return finalize_ranking(result, sort_by=sort_by)


def finalize_ranking(items, *, sort_by="priority"):
    prioritized = sort_ranked(items)
    ready_count = sum(item["ranking"]["status"] == "ready_to_test" for item in prioritized)
    for index, item in enumerate(prioritized):
        item["ranking"]["priority_rank"] = index + 1
        # Compatibility numeric field: an ordinal among ready investigations,
        # never dollars, saved time, utility, or an auto-application threshold.
        item["priority_score"] = max(0, ready_count - index)
        item["ranking"]["score_meaning"] = "ordinal_among_ready_findings_zero_when_ineligible"
    return sort_ranked(prioritized, sort_by)


def cluster_ranking(members, *, selection_cache=None):
    """Aggregate conservatively and reuse compatible-span opportunity selection."""
    members = sorted(
        members,
        key=lambda item: (
            str(item.get("project_id", "")),
            item["run_id"],
            item.get("finding_id", ""),
        ),
    )
    ranks, per_run, observed = [], {}, {}
    conflicting_spans = False
    for member in members:
        finding = member.get("finding")
        finding = finding if isinstance(finding, dict) else {}
        previous = _mapping(finding.get("ranking"))
        rank = finding_ranking(finding, inputs=previous.get("source_inputs", {}))
        old_invalid = previous.get("invalid_inputs", ())
        if isinstance(old_invalid, (list, tuple)):
            for name in old_invalid:
                if isinstance(name, str) and _KEY.fullmatch(name):
                    rank["reasons"].append("invalid_input:" + name)
                    rank["status"] = "needs_evidence_or_review"
        ranks.append(rank)
        key = (member.get("project_id"), member["run_id"])
        per_run.setdefault(key, []).append(
            SavingsItem(
                frozenset(finding.get("affected_spans") or ()),
                rank["components"]["latency"]["value"] or 0,
                rank["components"]["cost"]["value"] or 0,
            )
        )
        for row in finding.get("evidence", ()):
            if (
                isinstance(row, dict)
                and row.get("span_id") in (finding.get("affected_spans") or ())
                and _number(row.get("duration_ms"))
            ):
                span_key = (key, row["span_id"])
                if span_key in observed and observed[span_key] != row["duration_ms"]:
                    conflicting_spans = True
                observed[span_key] = row["duration_ms"]
    result = deepcopy(ranks[0])
    components = result["components"]
    reasons = set(reason for rank in ranks for reason in rank["reasons"])
    if conflicting_spans:
        reasons.add("conflicting_span_observations")
    required = set(name for rank in ranks for name in rank["required_inputs"])
    for key in (
        "quality_risk",
        "reversible",
        "validation_effort",
        "validation_cost",
        "confidence",
        "evidence",
    ):
        values = [rank["components"][key]["value"] for rank in ranks]
        value = None
        if all(item is not None for item in values):
            if key == "quality_risk":
                value = max(values, key={"low": 0, "medium": 1, "high": 2}.get)
            elif key == "confidence":
                value = min(values, key=_CONFIDENCE.get)
            elif key == "evidence":
                value = min(values, key=_EVIDENCE.get)
            elif key == "reversible":
                value = all(values)
            else:
                value = max(values)
        components[key] = _component(value, "declared", "conservative_member_aggregation")
    totals = []
    for key, items in per_run.items():
        cached = (selection_cache or {}).get(key)
        totals.append(
            cached[1]
            if cached is not None and Counter(cached[0]) == Counter(items)
            else select_compatible(items)
        )
    for key, attribute in (("latency", "latency_ms"), ("cost", "cost_usd")):
        components[key] = _component(
            _sum(getattr(selection, attribute) for selection in totals)
            if all(rank["components"][key]["value"] is not None for rank in ranks)
            else None,
            "estimated",
            "compatible_span_selection_per_run",
        )
    components["tokens"] = _component(None, "estimated", "see_member_estimates")
    components["impact"] = _component(
        _sum(observed.values())
        if not conflicting_spans
        and all(rank["components"]["impact"]["value"] is not None for rank in ranks)
        else None,
        "observed",
        "distinct_recorded_spans_per_run_nonadditive",
    )
    components["frequency"] = _component(len(per_run), "observed", "included_distinct_project_runs")
    if components["cost"]["value"] is None:
        reasons.add("cost_opportunity_unavailable")
    if components["impact"]["value"] is None:
        reasons.add("span_evidence_incomplete")
    if not all(selection.optimal for selection in totals):
        reasons.add("opportunity_selection_approximate")
    _units(components)
    result.update(
        status="ready_to_test" if not reasons else "needs_evidence_or_review",
        reasons=sorted(reasons),
        required_inputs=sorted(required),
        members=[
            {
                "run_id": member["run_id"],
                "project_id": member.get("project_id"),
                "finding_id": member.get("finding_id"),
                "ranking": rank,
            }
            for member, rank in zip(members, ranks)
        ],
        estimate_provenance=[rank["estimate_provenance"] for rank in ranks],
        source_inputs={},
        selection={
            "optimal": all(selection.optimal for selection in totals),
            "algorithms": sorted({selection.algorithm for selection in totals}),
        },
    )
    return result
