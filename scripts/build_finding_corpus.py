"""Build synthetic labeled fixtures, independently of detector outputs.

Regenerating the frozen corpus changes its hash and requires baseline review.
Local fake judgments are evidence fixtures, never the source of their labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgmentAnswer,
    LocalCallbackJudge,
    content_metadata,
)
from agentloop.events import AgentEvent
from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.semantic_waste import evaluate_semantic_waste
from agentloop.semantic_waste_types import SemanticInvestigation


def trace_fixture(identity, *, kind="tool_call", count=3, metadata=None):
    trace = AgentTrace(
        "Synthetic finding contract fixture",
        run_id=identity,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        elapsed_ms=1000,
        metadata={"synthetic": True},
    )
    for index in range(count):
        trace.events.append(
            AgentEvent(
                f"span-{index}",
                identity,
                kind,
                "lookup",
                f"2026-01-01T00:00:00.{index * 10000:06d}+00:00",
                f"2026-01-01T00:00:00.{(index + 1) * 10000:06d}+00:00",
                10,
                model="synthetic-mini" if kind == "model_call" else None,
                input_tokens=10 if kind == "model_call" else 0,
                output_tokens=2 if kind == "model_call" else 0,
                token_provenance="provider" if kind == "model_call" else "unavailable",
                input_text="same complete fixture input",
                output_text="same fixture result",
                metadata={
                    **({"provider_reported_cost_usd": 0.001} if kind == "model_call" else {}),
                    **(metadata or {}),
                },
            )
        )
    return trace


def make_corpus():
    cases = []

    def add(
        trace,
        rule,
        label,
        rationale,
        *,
        targets=None,
        evidence=(),
        scenario="ordinary",
        split="evaluation",
        group=None,
    ):
        cases.append(
            {
                "id": trace.run_id,
                "workload_ref": f"synthetic:{trace.run_id}:v1",
                "group": group or trace.run_id,
                "split": split,
                "scenario": scenario,
                "rule_id": rule,
                "label": label,
                "label_ref": "repository:scripts/build_finding_corpus.py:independent-contract-labels",
                "label_version": "1.0",
                "rationale": rationale,
                "opportunities": [
                    {"family": rule, "spans": targets or [event.event_id for event in trace.events]}
                ]
                if label == "opportunity"
                else [],
                "required_evidence": list(evidence),
                "trace": trace.to_dict(),
            }
        )

    for split, count in (("development", 4), ("evaluation", 3)):
        trace = trace_fixture(
            f"parallel-independent-{split}",
            count=count,
            metadata={"parallel_safe": True, "depends_on": []},
        )
        add(
            trace,
            "parallelize_tools",
            "opportunity",
            "Read-only independent lookups have complete serial timing and explicit safety declarations.",
            evidence=("timing",),
            split=split,
        )
        trace = trace_fixture(
            f"parallel-dependent-{split}", count=count, metadata={"parallel_safe": True}
        )
        for index, event in enumerate(trace.events[1:], 1):
            event.metadata["depends_on"] = [f"span-{index - 1}"]
        add(
            trace,
            "parallelize_tools",
            "no_opportunity",
            "Every later lookup consumes the preceding result; dependencies override the safety declaration.",
            evidence=("timing",),
            scenario="conflicting_evidence",
            split=split,
        )

    trace = trace_fixture("parallel-missing-timing", metadata={"parallel_safe": True})
    trace.events[0].started_at = trace.events[0].ended_at
    add(
        trace,
        "parallelize_tools",
        "unknown",
        "An inconsistent clock prevents confirming the serial opportunity.",
        evidence=("timing",),
        scenario="partial_telemetry",
    )
    trace = trace_fixture("parallel-unsafe", metadata={"parallel_safe": False})
    add(
        trace,
        "parallelize_tools",
        "no_opportunity",
        "The workload declares shared mutable state.",
        scenario="invalid_opportunity",
    )

    trace = trace_fixture("batch-independent", kind="model_call")
    add(
        trace,
        "batch_model_calls",
        "opportunity",
        "The fixture task consists of three independent items with a common schema and an available batch implementation.",
        evidence=("timing", "tokens", "cost"),
    )
    trace = trace_fixture("batch-stateful-shift", kind="model_call")
    for index, event in enumerate(trace.events[1:], 1):
        event.metadata["depends_on"] = [f"span-{index - 1}"]
    add(
        trace,
        "batch_model_calls",
        "no_opportunity",
        "The shifted workload has the same role names but each model call needs the preceding output; a single batch cannot preserve the computation.",
        scenario="workload_shift",
    )
    trace = trace_fixture(
        "schema-repair", kind="retry", count=1, metadata={"retry_reason": "invalid_output_schema"}
    )
    add(
        trace,
        "add_schema_validation",
        "opportunity",
        "The fixture explicitly reruns only because required structured fields are absent.",
        evidence=("timing",),
    )
    trace = trace_fixture(
        "network-retry", kind="retry", count=1, metadata={"retry_reason": "transport_timeout"}
    )
    add(
        trace,
        "add_schema_validation",
        "no_opportunity",
        "Schema validation cannot prevent the fixture's transport timeout.",
        scenario="invalid_opportunity",
    )

    families = (
        "semantic_redundancy",
        "low_contribution",
        "semantic_no_progress",
        "retry_usefulness",
        "context_relevance",
    )
    for family in families:
        for mode in ("supported", "retained", "disagreement", "unknown"):
            trace = trace_fixture(f"{family}-{mode}", kind="model_call")
            if family == "retry_usefulness":
                trace.events[1].metadata["retry_of"] = "span-0"
            targets = ("span-1", "span-2") if family == "semantic_no_progress" else ("span-1",)
            references = ("span-2",) if family == "low_contribution" else ("span-0",)
            case = SemanticInvestigation(
                trace.run_id,
                family,
                targets,
                references,
                "Preserve the independent fixture route and required verification",
                "fixture:route:v1",
                summaries={span: "Approved fixture summary" for span in (*targets, *references)},
                retention_required=mode == "retained",
            )
            answers = (True, False) if mode == "disagreement" else (True,)
            judges = [
                LocalCallbackJudge(
                    JudgeIdentity.configured(
                        f"synthetic-judge-{index}", "1", {"answer": answer, "simulated": True}
                    ),
                    lambda request, answer=answer, **kwargs: JudgmentAnswer(answer),
                )
                for index, answer in enumerate(answers)
            ]
            evaluate_semantic_waste(trace, [case], judges=judges, enabled=mode != "unknown")
            label = {
                "supported": "opportunity",
                "retained": "no_opportunity",
                "disagreement": "ambiguous",
                "unknown": "unknown",
            }[mode]
            rationale = {
                "supported": "The independently specified fixture makes the selected work unnecessary to its final route and has no verification or side-effect requirement.",
                "retained": "The caller explicitly requires this verification even when its answer repeats earlier evidence.",
                "disagreement": "Conflicting saved backend evidence is unresolved; no correctness label is assigned.",
                "unknown": "No semantic evidence was obtained; no correctness label is assigned.",
            }[mode]
            add(
                trace,
                family,
                label,
                rationale,
                targets=list(targets),
                evidence=("semantic",),
                scenario=mode,
            )

    return FindingBenchmark(
        {
            "schema_version": "1.0",
            "name": "Synthetic finding trust regression",
            "version": "1.0",
            "provenance_ref": "repository:scripts/build_finding_corpus.py:v1",
            "synthetic": True,
            "policy": {
                "id": "canonical finding rules",
                "version": "1.0",
                "ref": "git:41e0cc58274b9a257892fae29b697110cc3283f9; no threshold or prompt tuning",
            },
            "cases": cases,
        }
    )


def _content_trace(identity, steps):
    """A trace whose spans carry keyed content fingerprints.

    Each step is ``(span_id, kind, name, input, output, options)``. Options may
    set ``parent`` and ``required``. An ``input`` of ``None`` records no
    fingerprint for that span. Values are short synthetic placeholders.
    """
    trace = AgentTrace(
        "Synthetic content fingerprint fixture",
        run_id=identity,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        elapsed_ms=1000,
        metadata={"synthetic": True},
    )
    for index, (span_id, kind, name, value_in, value_out, options) in enumerate(steps):
        model = kind == "model_call"
        fingerprints = (
            {}
            if value_in is None
            else content_metadata(
                input=value_in,
                output=value_out,
                required=options.get("required", False),
                trace=trace,
            )
        )
        trace.events.append(
            AgentEvent(
                span_id,
                identity,
                kind,
                name,
                f"2026-01-01T00:00:00.{index * 10000:06d}+00:00",
                f"2026-01-01T00:00:00.{(index + 1) * 10000:06d}+00:00",
                10,
                parent_id=options.get("parent"),
                model="synthetic-large" if model else None,
                input_tokens=10 if model else 0,
                output_tokens=2 if model else 0,
                token_provenance="provider" if model else "unavailable",
                metadata={
                    **({"provider_reported_cost_usd": 0.001} if model else {}),
                    **fingerprints,
                },
            )
        )
    return trace


def make_content_corpus():
    """Labeled cases for the content fingerprint rules.

    Labels describe what each synthetic workflow does, written from its step list
    before the rules were run on it: whether a step returned its input unchanged
    or found nothing new, whether the caller requires it, and whether the
    evidence is missing or open to interpretation.
    """
    cases = []

    def add(trace, rule, label, rationale, *, targets=(), scenario="ordinary", split="evaluation"):
        cases.append(
            {
                "id": trace.run_id,
                "workload_ref": f"synthetic:{trace.run_id}:v1",
                "group": trace.run_id,
                "split": split,
                "scenario": scenario,
                "rule_id": rule,
                "label": label,
                "label_ref": "repository:scripts/build_finding_corpus.py:content-fingerprint-labels",
                "label_version": "1.0",
                "rationale": rationale,
                "opportunities": [{"family": rule, "spans": list(targets)}]
                if label == "opportunity"
                else [],
                "required_evidence": ["content"],
                "trace": trace.to_dict(),
            }
        )

    model, tool = "model_call", "tool_call"

    def solve(prefix, answer, checked, **options):
        return [
            (f"{prefix}draft", model, "solve.draft", answer and "problem", answer, options),
            (f"{prefix}verify", model, "solve.verify", answer, checked, options),
        ]

    def tasks(count, build):
        steps = []
        for task in range(count):
            steps.append((f"task-{task}", tool, "task", None, None, {}))
            steps += build(task, {"parent": f"task-{task}"})
        return steps

    # unchanged_output
    add(
        _content_trace("unchanged-verify-development", solve("", "17", "17")),
        "unchanged_output",
        "opportunity",
        "The final check returned the draft answer unchanged; nothing requires it to run.",
        targets=["verify"],
        split="development",
    )
    add(
        _content_trace("unchanged-verify-single", solve("", "answer 42", "answer 42")),
        "unchanged_output",
        "opportunity",
        "The trailing verification call handed back its draft unchanged and is not required.",
        targets=["verify"],
    )
    add(
        _content_trace(
            "unchanged-verify-batch",
            tasks(
                4, lambda task, opts: solve(f"t{task}-", f"value {task}", f"value {task}", **opts)
            ),
        ),
        "unchanged_output",
        "opportunity",
        "Across four tasks the verification call never changed the draft.",
        targets=[f"t{task}-verify" for task in range(4)],
    )
    add(
        _content_trace("unchanged-verify-fixes", solve("", "draft 9", "corrected 12")),
        "unchanged_output",
        "no_opportunity",
        "The verification call replaced the draft, so it changed the result.",
        scenario="invalid_opportunity",
    )
    add(
        _content_trace(
            "unchanged-verify-required", solve("", "approved", "approved", required=True)
        ),
        "unchanged_output",
        "no_opportunity",
        "The caller declares this check mandatory even when it confirms the draft.",
        scenario="retained",
    )
    pairs = [("a", "a"), ("b", "b2"), ("c", "c2"), ("d", "d2"), ("e", "e")]
    add(
        _content_trace(
            "unchanged-verify-mostly-fixes",
            tasks(5, lambda task, opts: solve(f"t{task}-", *pairs[task], **opts)),
        ),
        "unchanged_output",
        "no_opportunity",
        "The check revised three of five drafts; removing it would change most results.",
        scenario="conflicting_evidence",
    )
    add(
        _content_trace("unchanged-verify-case-only", solve("", "Paris", "paris")),
        "unchanged_output",
        "ambiguous",
        "The check changed only letter case; whether that matters depends on an unstated scorer.",
        scenario="disagreement",
    )
    add(
        _content_trace("unchanged-verify-no-fingerprints", solve("", None, None)),
        "unchanged_output",
        "unknown",
        "No content fingerprints were recorded, so nothing shows whether the check changed the draft.",
        scenario="partial_telemetry",
    )

    # repeated_result
    def rephrase(prefix, first, second, **options):
        query = None if first is None else "search query"
        return [
            (
                f"{prefix}find-1",
                tool,
                "find",
                None if first is None else "question",
                first,
                options,
            ),
            (
                f"{prefix}rephrase",
                model,
                "rephrase",
                None if first is None else "question",
                query,
                options,
            ),
            (f"{prefix}find-2", tool, "find", query, second, options),
            (
                f"{prefix}answer",
                model,
                "answer",
                None if first is None else "passages",
                "final",
                options,
            ),
        ]

    add(
        _content_trace("repeated-retrieval-development", rephrase("", ["s1", "s2"], ["s1"])),
        "repeated_result",
        "opportunity",
        "The rephrased lookup returned a passage already found; the rephrase exists only to feed it.",
        targets=["rephrase", "find-2"],
        split="development",
    )
    add(
        _content_trace("repeated-retrieval-subset", rephrase("", ["s1", "s2", "s3"], ["s3"])),
        "repeated_result",
        "opportunity",
        "The second lookup found only a passage the first had already returned.",
        targets=["rephrase", "find-2"],
    )
    add(
        _content_trace("repeated-retrieval-new-passage", rephrase("", ["s1"], ["s1", "s4"])),
        "repeated_result",
        "no_opportunity",
        "The rephrased lookup found a passage the first lookup missed.",
        scenario="invalid_opportunity",
    )
    add(
        _content_trace(
            "repeated-retrieval-across-tasks",
            tasks(
                3,
                lambda task, opts: [
                    (f"t{task}-find-1", tool, "find", f"question {task}", ["shared"], opts),
                    (f"t{task}-find-2", tool, "find", f"query {task}", [f"new {task}"], opts),
                ],
            ),
        ),
        "repeated_result",
        "no_opportunity",
        "Each task's second lookup found something new; passages repeat only across unrelated tasks.",
        scenario="workload_shift",
    )
    steps = rephrase("", ["s1"], ["s1"])
    steps[3] = ("answer", model, "answer", "search query", "final", {})
    add(
        _content_trace("repeated-retrieval-shared-query", steps),
        "repeated_result",
        "opportunity",
        "The second lookup added nothing, but the answer step also uses the rephrased query, so only the lookup is removable.",
        targets=["find-2"],
        scenario="shared_state",
    )
    add(
        _content_trace("repeated-retrieval-no-fingerprints", rephrase("", None, None)),
        "repeated_result",
        "unknown",
        "No content fingerprints were recorded, so the lookups cannot be compared.",
        scenario="partial_telemetry",
    )
    add(
        _content_trace("repeated-retrieval-reordered", rephrase("", ["s1", "s2"], ["s2", "s1"])),
        "repeated_result",
        "ambiguous",
        "The second lookup returned the same passages in another order; whether order matters to the answer step is not recorded.",
        scenario="disagreement",
    )

    return FindingBenchmark(
        {
            "schema_version": "1.0",
            "name": "Synthetic content fingerprint finding regression",
            "version": "1.0",
            "provenance_ref": "repository:scripts/build_finding_corpus.py:content-v1",
            "synthetic": True,
            "policy": {
                "id": "content fingerprint rules",
                "version": "1.0",
                "ref": "unchanged_output 1.0 and repeated_result 1.0; thresholds fixed before these labels were written",
            },
            "cases": cases,
        }
    )


CORPORA = {"canonical": make_corpus, "content": make_content_corpus}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", choices=sorted(CORPORA), default="canonical")
    args = parser.parse_args()
    # Never overwrite reviewed fixtures or their corresponding release baseline.
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(CORPORA[args.corpus]().to_dict(), indent=2) + "\n")
