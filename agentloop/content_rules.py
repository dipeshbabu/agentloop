"""Deterministic findings from opt-in content fingerprints (``agentloop.content``).

Both rules compare keyed digests recorded by
:func:`agentloop.content_fingerprints.record_content` within one trace and one
fingerprint scope. Spans without a valid record are ignored, so a trace with no
records yields no finding. Neither rule sees text, so neither can tell whether a
step's output was *correct*; they only report that it was *unchanged* or
*already seen*. That is why confidence never exceeds ``medium``.

``unchanged_output``
    A model call whose output digest equals its input digest: a "check the
    answer" or "polish the draft" call that handed its draft back unchanged.
    Calls are grouped by span name. A group is reported when at least half of
    its fingerprinted calls were unchanged.

``repeated_result``
    A tool call (such as a retrieval) that, for a different input, returned nothing that earlier steps with
    the same name had not already returned: for example a second retrieval with
    a rephrased query that found the same passages. Calls are compared only with
    earlier calls under the same parent span, so record one task per trace or
    wrap each task in its own span. A model call whose output
    digest is that step's input (the rephrase that produced the query) is
    included when every fingerprinted step that consumed it was redundant.

Confidence is tied to how often the pattern held in the trace: ``medium`` when
it held on at least :data:`MEDIUM_MIN_SPANS` spans and on at least
:data:`MEDIUM_MIN_SHARE` of the eligible ones, otherwise ``low``.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from typing import Any

from agentloop.content_fingerprints import read_content
from agentloop.estimates import ESTIMATORS

MEDIUM_MIN_SPANS = 3
MEDIUM_MIN_SHARE = Fraction(4, 5)
_REPORT_MIN_SHARE = Fraction(1, 2)

_VALIDATION = (
    "Run a paired replay without the step (or with it gated) and keep the change only if "
    "an independent task-quality scorer shows no regression on held-out tasks."
)


def _confidence(held: int, eligible: int) -> str:
    if held >= MEDIUM_MIN_SPANS and Fraction(held, eligible) >= MEDIUM_MIN_SHARE:
        return "medium"
    return "low"


def _fingerprinted(graph) -> list[tuple[Any, dict[str, Any]]]:
    rows = []
    for node in graph.nodes:
        record = read_content(node.metadata)
        if record is not None and node.status == "ok":
            rows.append((node, record))
    return rows


def _order(node) -> tuple[str, str]:
    return (node.started_at or "", node.node_id)


def unchanged_output_cards(graph) -> list:
    from agentloop.rules import FindingCandidate, RecommendationType

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for node, record in _fingerprinted(graph):
        if (
            node.operation_kind == "model"
            and not record["required"]
            and "input" in record
            and "output" in record
        ):
            groups[(record["scope"], node.name)].append((node, record))
    cards = []
    for (_, name), rows in sorted(groups.items(), key=lambda item: item[0][1]):
        unchanged = [
            node for node, record in rows if record["input"]["digest"] == record["output"]["digest"]
        ]
        if not unchanged or Fraction(len(unchanged), len(rows)) < _REPORT_MIN_SHARE:
            continue
        unchanged.sort(key=_order)
        unchanged_ids = {node.node_id for node in unchanged}
        duration = sum(node.duration_ms for node in unchanged)
        cards.append(
            FindingCandidate(
                type=RecommendationType.UNCHANGED_OUTPUT,
                title=f"Gate or remove `{name}`: it returned its input unchanged",
                why=(
                    f"{len(unchanged)} of {len(rows)} fingerprinted `{name}` calls returned "
                    "exactly the value they were given (by keyed content fingerprint)."
                ),
                rewrite_hint=(
                    "Skip this step, or run it only when a cheap check says the draft needs it. "
                    "An unchanged output can still be a check that passed, so measure task "
                    "quality without the step before removing it."
                ),
                confidence=_confidence(len(unchanged), len(rows)),
                estimated_latency_savings_ms=duration
                * ESTIMATORS["unchanged_output"].parameter("latency_fraction"),
                affected_nodes=[node.node_id for node in unchanged],
                evidence_level="deterministic_inference",
                assumptions=[
                    "The recorded input is the value the step was asked to check or revise.",
                    "Calls whose output matched their input would have produced the same "
                    "final result without the step.",
                ],
                observations={
                    "basis": "content_fingerprint",
                    "span_name": name,
                    "fingerprinted_calls": len(rows),
                    "unchanged_calls": len(unchanged),
                    "changed_spans": sorted(
                        node.node_id for node, _ in rows if node.node_id not in unchanged_ids
                    ),
                    "validation_criteria": _VALIDATION,
                },
                estimate_formula="sum(unchanged call durations) * latency_fraction",
            )
        )
    return cards


def _seen(record: dict[str, Any]) -> set[str]:
    output = record["output"]
    return {output["digest"], *output.get("items", ())}


def _adds_nothing(record: dict[str, Any], seen: set[str], outputs: set[str]) -> bool:
    output = record["output"]
    if output["digest"] in outputs:
        return True
    items = output.get("items")
    return bool(items) and set(items) <= seen


def repeated_result_cards(graph) -> list:
    from agentloop.rules import FindingCandidate, RecommendationType

    rows = _fingerprinted(graph)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for node, record in rows:
        if node.operation_kind == "tool" and "input" in record and "output" in record:
            groups[(record["scope"], node.name)].append((node, record))
    cards = []
    for (scope, name), members in sorted(groups.items(), key=lambda item: item[0][1]):
        members.sort(key=lambda row: _order(row[0]))
        redundant, eligible = [], 0
        # Each parent span is one unit of work (for example one task); results seen
        # under another parent are not evidence that this call was redundant.
        units: dict[str | None, list] = defaultdict(list)
        for row in members:
            units[row[0].parent_id].append(row)
        for unit in units.values():
            seen: set[str] = set()
            outputs: set[str] = set()
            inputs: set[str] = set()
            for index, (node, record) in enumerate(unit):
                if index and record["input"]["digest"] not in inputs:
                    # Identical repeated requests are request-local reuse, reported elsewhere.
                    eligible += 1
                    if not record["required"] and _adds_nothing(record, seen, outputs):
                        redundant.append((node, record))
                inputs.add(record["input"]["digest"])
                outputs.add(record["output"]["digest"])
                seen |= _seen(record)
        if not redundant or Fraction(len(redundant), eligible) < _REPORT_MIN_SHARE:
            continue
        redundant_ids = {node.node_id for node, _ in redundant}
        consumers: dict[str, set[str]] = defaultdict(set)
        for node, record in rows:
            if record["scope"] == scope and "input" in record:
                consumers[record["input"]["digest"]].add(node.node_id)
        producers = []
        for node, record in rows:
            if (
                record["scope"] == scope
                and node.operation_kind == "model"
                and not record["required"]
                and "output" in record
                and node.node_id not in redundant_ids
            ):
                used_by = consumers.get(record["output"]["digest"], set())
                if used_by and used_by <= redundant_ids:
                    producers.append(node)
        affected = sorted([*producers, *(node for node, _ in redundant)], key=_order)
        duration = sum(node.duration_ms for node in affected)
        cards.append(
            FindingCandidate(
                type=RecommendationType.REPEATED_RESULT,
                title=f"Remove the extra `{name}` call that found nothing new",
                why=(
                    f"{len(redundant)} of {eligible} later `{name}` calls with a different input "
                    "returned only results that earlier calls had already returned "
                    "(by keyed content fingerprint)."
                    + (
                        f" {len(producers)} model call(s) produced only those inputs."
                        if producers
                        else ""
                    )
                ),
                rewrite_hint=(
                    "Drop the extra lookup and any step that only prepares its input, or run it "
                    "only when the first lookup comes back empty or weak."
                ),
                confidence=_confidence(len(redundant), eligible),
                estimated_latency_savings_ms=duration
                * ESTIMATORS["repeated_result"].parameter("latency_fraction"),
                affected_nodes=[node.node_id for node in affected],
                evidence_level="deterministic_inference",
                assumptions=[
                    "Results that were already returned add nothing when returned again.",
                    "The listed producer calls have no other use than preparing these inputs.",
                ],
                observations={
                    "basis": "content_fingerprint",
                    "span_name": name,
                    "later_calls_with_new_input": eligible,
                    "redundant_spans": sorted(redundant_ids),
                    "producer_spans": sorted(node.node_id for node in producers),
                    "validation_criteria": _VALIDATION,
                },
                estimate_formula="sum(redundant and producer call durations) * latency_fraction",
            )
        )
    return cards
