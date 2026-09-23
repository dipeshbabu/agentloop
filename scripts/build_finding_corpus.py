"""Build synthetic labeled fixtures, independently of detector outputs.

Regenerating the frozen corpus changes its hash and requires baseline review.
Local fake judgments are evidence fixtures, never the source of their labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import AgentTrace, JudgeIdentity, JudgmentAnswer, LocalCallbackJudge
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Never overwrite reviewed fixtures or their corresponding release baseline.
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(make_corpus().to_dict(), indent=2) + "\n")
