"""Optional offline investigations; report construction only reads saved evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

from agentloop.judgment_types import (
    JUDGMENT_KEY,
    JudgmentRequest,
    JudgmentSpec,
    canonical,
    fingerprint,
    finite,
)
from agentloop.judgments import (
    JudgmentSession,
    _bound,
    attach_judgment,
    judgment_request,
    read_judgments,
)
from agentloop.operations import operation_kind
from agentloop.semantic_waste_types import (
    QUESTIONS,
    SEMANTIC_WASTE_KEY,
    SEMANTIC_WASTE_VERSION,
    SemanticInvestigation,
)
from agentloop.timing import event_interval_ms


def _copy(value):
    return json.loads(canonical(value))


def _spec(case):
    question = (
        QUESTIONS[case.family]
        + f" The first {len(case.reference_spans)} evidence spans are reference context; "
        + f"the remaining {len(case.target_spans)} spans are targets. "
        + "Task criteria: "
        + case.task_criteria
        + " Criteria reference digest: "
        + fingerprint(case.criteria_ref)
    )
    return JudgmentSpec(question, "boolean")


def _deterministic_reuse(case, events):
    """Exact recorded equality under an explicit host reuse contract, not name counts."""
    if (
        case.family != "semantic_redundancy"
        or not case.reuse_safe
        or len(case.target_spans) != 1
        or len(case.reference_spans) != 1
    ):
        return False
    earlier = events.get(case.reference_spans[0])
    later = events.get(case.target_spans[0])
    if earlier is None or later is None:
        return False
    first, second = event_interval_ms(earlier), event_interval_ms(later)
    ignored = {
        "otel_span_id",
        "otel_trace_id",
        "provider_reported_cost_usd",
        "cost_usd",
        "cached_input_tokens",
    }
    same_context = fingerprint(
        {key: value for key, value in earlier.metadata.items() if key not in ignored}
    ) == fingerprint({key: value for key, value in later.metadata.items() if key not in ignored})
    return bool(
        first is not None
        and second is not None
        and first[1] <= second[0]
        and earlier.status == later.status == "ok"
        and earlier.name == later.name
        and earlier.model == later.model
        and same_context
        and earlier.input_tokens == later.input_tokens
        and earlier.output_tokens == later.output_tokens
        and operation_kind(earlier) == operation_kind(later)
        and isinstance(earlier.input_text, str)
        and bool(earlier.input_text)
        and isinstance(earlier.output_text, str)
        and bool(earlier.output_text)
        and earlier.input_text == later.input_text
        and earlier.output_text == later.output_text
    )


def _scope_gap(case, events):
    targets = [events[identity] for identity in case.target_spans]
    references = [events[identity] for identity in case.reference_spans]
    target_times = [event_interval_ms(item) for item in targets]
    reference_times = [event_interval_ms(item) for item in references]
    if case.family == "retry_usefulness":
        if not all(
            item.event_type == "retry" or item.metadata.get("retry_of") in case.reference_spans
            for item in targets
        ):
            return "missing_retry_relation"
    if case.family in {"semantic_redundancy", "low_contribution", "semantic_no_progress"}:
        if any(item is None for item in target_times + reference_times):
            return "missing_order_evidence"
        if case.family == "semantic_redundancy" and max(item[1] for item in reference_times) > min(
            item[0] for item in target_times
        ):
            return "reference_not_available"
        if case.family == "low_contribution" and not any(
            item[0] >= max(target[1] for target in target_times) for item in reference_times
        ):
            return "missing_downstream_evidence"
        if case.family == "semantic_no_progress" and any(
            left[1] > right[0] for left, right in zip(target_times, target_times[1:])
        ):
            return "unordered_sequence"
    if case.family == "context_relevance" and not all(
        item.event_type == "model_call" or operation_kind(item) in {"model", "classifier", "rule"}
        for item in targets
    ):
        return "ineligible_context_target"
    return None


def evaluate_semantic_waste(trace, investigations, *, judges=(), enabled=False, timeout_s=None):
    """Attach new purpose-bound investigations; no runtime action is proposed or run.

    Source traces must remain unchanged during this offline operation. Prior
    investigation IDs cannot be replaced. Use a new ID to record a reevaluation.
    Approved criteria/summaries are the only caller text forwarded to a judge.
    """
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    if timeout_s is not None and (not finite(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout_s must be finite and positive")
    if (
        not isinstance(investigations, (tuple, list))
        or not investigations
        or len(investigations) > 100
        or any(type(item) is not SemanticInvestigation for item in investigations)
    ):
        raise ValueError("supply between one and 100 SemanticInvestigation objects")
    if not isinstance(judges, (tuple, list)) or len(judges) > 8:
        raise ValueError("judges must be a list or tuple of at most eight backends")
    identities = [item.investigation_id for item in investigations]
    if len(set(identities)) != len(identities):
        raise ValueError("investigation IDs must be unique")
    old = trace.metadata.get(SEMANTIC_WASTE_KEY)
    if old is not None:
        current = read_semantic_waste(trace)
        if current["status"] in {"invalid", "unsupported"}:
            raise ValueError("existing semantic investigations are invalid or unsupported")
        if set(identities) & {item["definition"]["investigation_id"] for item in old["cases"]}:
            raise ValueError("use a new investigation ID rather than replacing saved evidence")
    elif SEMANTIC_WASTE_KEY in trace.metadata:
        raise ValueError("existing semantic investigations are invalid")
    working = SimpleNamespace(
        run_id=trace.run_id, events=trace.events, metadata=deepcopy(trace.metadata)
    )
    sessions = [JudgmentSession(backend) for backend in judges]
    events = {event.event_id: event for event in trace.events}
    cases = []
    for case in investigations:
        row = {
            "definition": case.to_dict(),
            "request": None,
            "judgment_hashes": [],
            "basis": "unavailable",
            "reason": None,
        }
        try:
            request = judgment_request(
                trace, _spec(case), list(case.evidence_spans), summaries=dict(case.summaries)
            )
        except ValueError:
            row["reason"] = "missing_or_invalid_evidence"
            cases.append(row)
            continue
        row["request"] = request.to_dict()
        if case.retention_required:
            row["reason"] = "retention_required"
        elif _scope_gap(case, events) is not None:
            row["reason"] = _scope_gap(case, events)
        elif _deterministic_reuse(case, events):
            row["basis"] = "deterministic_reuse"
        elif set(dict(case.summaries)) != set(case.evidence_spans):
            row["reason"] = "missing_approved_summaries"
        elif not sessions:
            row["reason"] = "no_judge"
        else:
            row["basis"] = "semantic_judgment"
            for session in sessions:
                receipt = session.evaluate(request, enabled=enabled, timeout_s=timeout_s)
                attach_judgment(working, receipt)
                row["judgment_hashes"].append(receipt["record_hash"])
        cases.append(row)
    envelope = {
        "schema_version": SEMANTIC_WASTE_VERSION,
        "run_id": trace.run_id,
        "cases": [*(old["cases"] if old is not None else []), *cases],
    }
    envelope["evidence_hash"] = fingerprint(envelope)
    working.metadata[SEMANTIC_WASTE_KEY] = envelope
    checked = read_semantic_waste(working)
    if checked["status"] == "invalid" or any(
        item["status"] == "stale" for item in checked["cases"][-len(cases) :]
    ):
        raise ValueError("source evidence changed during the investigation")
    if JUDGMENT_KEY in working.metadata:
        trace.metadata[JUDGMENT_KEY] = working.metadata[JUDGMENT_KEY]
    trace.metadata[SEMANTIC_WASTE_KEY] = envelope
    return checked


def read_semantic_waste(trace, *, judgments=None):
    """Validate the purpose, source and judge bindings without executing a backend."""
    metadata = getattr(trace, "metadata", {})
    if SEMANTIC_WASTE_KEY not in metadata:
        return None
    empty = {"status": "invalid", "cases": []}
    try:
        envelope = _copy(metadata[SEMANTIC_WASTE_KEY])
        if not isinstance(envelope, dict):
            return empty
        if envelope.get("schema_version") != SEMANTIC_WASTE_VERSION:
            return {**empty, "status": "unsupported"}
        digest = envelope.pop("evidence_hash")
        if digest != fingerprint(envelope) or not isinstance(envelope.get("cases"), list):
            return empty
        if judgments is None:
            judgments = read_judgments(trace)
        receipts = {item["record_hash"]: item for item in (judgments or {}).get("records", [])}
        events = {event.event_id: event for event in trace.events}
        source_hashes, seen, cases = {}, set(), []
        for raw in envelope["cases"]:
            case = SemanticInvestigation.from_dict(raw["definition"])
            if case.investigation_id in seen:
                return empty
            seen.add(case.investigation_id)
            hashes = raw["judgment_hashes"]
            if (
                not isinstance(hashes, list)
                or any(not isinstance(value, str) for value in hashes)
                or len(set(hashes)) != len(hashes)
            ):
                return empty
            row = {**raw, "status": "unknown", "confidence": "unknown", "judgments": []}
            if envelope["run_id"] != trace.run_id:
                row["status"] = "stale"
            elif raw["request"] is None:
                if raw["basis"] != "unavailable" or hashes:
                    return empty
            else:
                request = JudgmentRequest.from_dict(raw["request"])
                if (
                    request.spec != _spec(case)
                    or tuple(item.span_id for item in request.evidence) != case.evidence_spans
                    or {
                        item.span_id: item.summary
                        for item in request.evidence
                        if item.summary is not None
                    }
                    != dict(case.summaries)
                ):
                    return empty
                if not _bound(
                    trace, {"request": raw["request"]}, events=events, hashes=source_hashes
                ):
                    row["status"] = "stale"
                elif case.retention_required:
                    row["status"] = "retained"
                elif _scope_gap(case, events) is not None:
                    row["reason"] = _scope_gap(case, events)
                elif raw["basis"] == "deterministic_reuse":
                    if hashes or not _deterministic_reuse(case, events):
                        return empty
                    row.update(status="supported", confidence="medium")
                elif raw["basis"] == "semantic_judgment":
                    if not hashes:
                        return empty
                    selected = [receipts.get(value) for value in hashes]
                    if any(item is None or item["request"] != raw["request"] for item in selected):
                        row["reason"] = "missing_or_mismatched_judgment"
                    else:
                        row["judgments"] = selected
                        if any(item["effective_status"] != "known" for item in selected):
                            row["reason"] = "unknown_or_failed_judgment"
                        elif len({item["effective_value"] for item in selected}) != 1:
                            row["status"] = "disagreement"
                        elif not selected[0]["effective_value"]:
                            row["status"] = "not_supported"
                        elif any(
                            item["evaluation"]["uncertainty"]["confidence"] is not None
                            and item["evaluation"]["uncertainty"]["confidence"]
                            < case.minimum_confidence
                            for item in selected
                        ):
                            row["status"] = "low_confidence"
                        else:
                            row.update(status="supported", confidence="low")
                elif raw["basis"] != "unavailable" or hashes:
                    return empty
            cases.append(row)
        return {
            "status": "complete"
            if cases
            and all(item["status"] in {"supported", "not_supported", "retained"} for item in cases)
            else "incomplete",
            "cases": cases,
            "evidence_hash": digest,
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return empty


def semantic_waste_markdown(evidence):
    from agentloop.markdown import markdown_table_cell

    lines = [
        "",
        "## Offline semantic investigations",
        "",
        "Observed facts, deterministic inferences and judge opinions are distinct. Unsupported cases do not produce findings; conditional savings require independent paired replay.",
        "",
        f"Investigation completeness: {markdown_table_cell(evidence['status'])}",
        "",
        "| Investigation | Family | Status | Basis | Task criteria reference | Judge references |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in evidence["cases"]:
        case = item["definition"]
        cells = [
            case["investigation_id"],
            case["family"],
            item["status"],
            item["basis"],
            case["criteria_ref"],
            ", ".join(item["judgment_hashes"]) or "none",
        ]
        lines.append("| " + " | ".join(markdown_table_cell(str(value)) for value in cells) + " |")
    return lines
