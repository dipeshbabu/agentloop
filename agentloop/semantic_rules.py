"""Canonical finding candidates from validated, explicitly scoped investigations."""

from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

from agentloop.costs import is_cost_evaluable
from agentloop.semantic_waste_types import SemanticInvestigation
from agentloop.timing import event_interval_ms
from agentloop.tokens import token_status

_TITLES = {
    "semantic_redundancy": "Investigate semantically redundant work",
    "low_contribution": "Investigate low-contribution work",
    "semantic_no_progress": "Investigate a sequence with no task progress",
    "retry_usefulness": "Investigate retries without useful new evidence",
    "context_relevance": "Investigate context unrelated to the decision",
}


def _total(values):
    try:
        return float(sum((Fraction(str(value)) for value in values), Fraction()))
    except (ValueError, OverflowError):
        return None


def _inputs(context):
    events = {item["event_id"]: item for item in context.report["events"]}
    children = {item.get("parent_id") for item in events.values() if item.get("parent_id")}
    model_events = [item for item in events.values() if item["event_type"] == "model_call"]
    model_costs = context.report.get("cost_breakdown", {}).get("model_calls", [])
    return {
        "events": events,
        "children": children,
        "model_costs": {event["event_id"]: cost for event, cost in zip(model_events, model_costs)}
        if len(model_events) == len(model_costs)
        else {},
    }


def _estimate(context, case, row, inputs):
    events = inputs["events"]
    selected = [events[identity] for identity in case.target_spans]
    leaf = all(item["event_id"] not in inputs["children"] for item in selected)
    all_models = all(item["event_type"] == "model_call" for item in selected)
    selected_token_status = (
        token_status([SimpleNamespace(**item) for item in selected]) if all_models else None
    )
    eligible = bool(case.removal_attribution_ref and leaf and case.family != "context_relevance")
    predictions = {
        "latency_ms": None,
        "cost_usd": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    assumptions = [
        "Selected evidence and approved summaries cover the relevant task context and downstream outcomes.",
        "The finding is a hypothesis; independent paired replay must preserve the caller's quality criteria.",
    ]
    if row["basis"] == "deterministic_reuse":
        assumptions.append(
            "The referenced host reuse contract guarantees complete inputs, unchanged operation semantics, and safe result reuse without required side effects or independent verification."
        )
    costs = []
    serial = False
    if eligible:
        assumptions.extend(
            [
                "The caller's removal attribution applies to every selected leaf span, including side effects and downstream dependencies.",
                "Removing those spans requires no replacement work, cache lookup, repair, or additional validation overhead.",
                "Resource estimates are conditional upper bounds on recorded work, not realized savings or a correctness guarantee.",
            ]
        )
        if "serial" not in inputs:
            intervals = [event_interval_ms(SimpleNamespace(**item)) for item in events.values()]
            details = context.graph.dependency_summary()
            inputs["serial"] = False
            if (
                all(value is not None for value in intervals)
                and all(
                    item["duration_ms"] > 0 or interval[0] == interval[1]
                    for item, interval in zip(events.values(), intervals)
                )
                and all(not item.get("parent_id") for item in events.values())
                and (details is None or details["valid"])
            ):
                ordered = sorted(intervals)
                inputs["serial"] = all(
                    left[1] <= right[0] for left, right in zip(ordered, ordered[1:])
                )
        serial = inputs["serial"]
        if serial:
            predictions["latency_ms"] = _total(item["duration_ms"] for item in selected)
            assumptions.append(
                "The recorded flat, nonoverlapping execution remains serial after the change, so removed span time can shorten elapsed runtime."
            )
        if all_models:
            exact_tokens = selected_token_status in {
                "exact",
                "empty",
            }
            if exact_tokens:
                predictions["input_tokens"] = sum(item["input_tokens"] for item in selected)
                predictions["output_tokens"] = sum(item["output_tokens"] for item in selected)
            by_id = inputs["model_costs"]
            if all(item["event_id"] in by_id for item in selected):
                costs = [
                    {"span_id": item["event_id"], **by_id[item["event_id"]]} for item in selected
                ]
                if is_cost_evaluable(context.report.get("cost_status")) and all(
                    item["state"] == "provider_reported"
                    or (item["state"] == "calculated" and exact_tokens)
                    for item in costs
                ):
                    predictions["cost_usd"] = _total(item["amount_usd"] for item in costs)
    return {
        "estimator_id": "semantic_leaf_removal",
        "estimator_version": "1.0",
        "method": "conditional_upper_bound" if eligible else "unavailable_attribution",
        "confidence": row["confidence"],
        "calibrated": False,
        "formula": "conditional removal: sum selected nonoverlapping serial leaf durations; sum separately attributed model billing and exact model token counts",
        "parameters": {},
        "assumptions": assumptions,
        "inputs": {
            "family": case.family,
            "attribution_eligible": eligible,
            "all_model_calls": all_models,
            "token_status": selected_token_status,
            "trace_cost_status": context.report.get("cost_status"),
            "target_spans": list(case.target_spans),
            "leaf_spans": leaf,
            "flat_serial_timing": serial,
            "removal_attribution_ref": case.removal_attribution_ref,
            "recorded_target_work_ms": _total(item["duration_ms"] for item in selected),
            "model_costs": costs,
        },
        "predictions": predictions,
        "unmodeled_metrics": [key for key, value in predictions.items() if value is None],
    }


def semantic_candidates(context, family):
    from agentloop.rules import FindingCandidate, RecommendationType

    evidence = context.report.get("semantic_waste")
    if not evidence or evidence["status"] in {"invalid", "unsupported"}:
        return []
    result = []
    inputs = None
    for row in evidence["cases"]:
        if row["status"] != "supported" or row["definition"]["family"] != family:
            continue
        case = SemanticInvestigation.from_dict(row["definition"])
        if inputs is None:
            inputs = _inputs(context)
        estimate = _estimate(context, case, row, inputs)
        observations = {
            "investigation_id": case.investigation_id,
            "criteria_ref": case.criteria_ref,
            "reference_spans": list(case.reference_spans),
            "target_spans": list(case.target_spans),
            "evidence_hash": evidence["evidence_hash"],
            "basis": row["basis"],
            "reuse_contract_ref": case.reuse_contract_ref,
            "judgments": [
                {
                    "record_hash": item["record_hash"],
                    "judge": item["judge"],
                    "uncertainty": item["evaluation"]["uncertainty"],
                    "status": item["effective_status"],
                    "value": item["effective_value"],
                    "evidence_refs": [part["span_id"] for part in item["request"]["evidence"]],
                }
                for item in row["judgments"]
            ],
            "validation_criteria": "Run a paired intervention against the original trace, with an independent scorer implementing the referenced task criteria, preserved required side effects, and complete cost/token provenance.",
        }
        result.append(
            FindingCandidate(
                type=RecommendationType(family),
                title=_TITLES[family],
                why="An explicit investigation supports this hypothesis under the recorded task criteria and evidence coverage assumptions.",
                rewrite_hint="Prepare an offline candidate and validate its independent task quality before accepting any step removal or context change.",
                confidence=row["confidence"],
                affected_nodes=list(case.target_spans),
                estimated_latency_savings_ms=estimate["predictions"]["latency_ms"],
                estimated_cost_savings_usd=estimate["predictions"]["cost_usd"],
                evidence_level="deterministic_inference"
                if row["basis"] == "deterministic_reuse"
                else "semantic_judgment",
                assumptions=estimate["assumptions"],
                observations=observations,
                estimate_formula=estimate["formula"],
                estimate=estimate,
            )
        )
    return result
