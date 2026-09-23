"""Synthetic offline fixtures for all five semantic finding families and abstentions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentUncertainty,
    LocalCallbackJudge,
    SemanticInvestigation,
    build_diagnosis,
    evaluate_semantic_waste,
)
from agentloop.entrypoint import _analysis_payload
from agentloop.events import AgentEvent
from agentloop.findings import diagnosis_to_markdown
from agentloop.html_report import analysis_to_html
from agentloop.semantic_waste_types import QUESTIONS


def fixture_trace():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = AgentTrace(
        "synthetic semantic waste cases",
        run_id="semantic-waste-fixtures",
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=100)).isoformat(),
        elapsed_ms=100,
        metadata={"synthetic": True, "source": "agentloop_semantic_waste_fixture"},
    )
    names = (
        "earlier",
        "duplicate",
        "low",
        "final",
        "step1",
        "step2",
        "failed",
        "retry",
        "context",
        "decision",
    )
    for index, identity in enumerate(names):
        metadata = {"provider_reported_cost_usd": 0.001}
        if identity == "retry":
            metadata["retry_of"] = "failed"
        trace.events.append(
            AgentEvent(
                identity,
                trace.run_id,
                "model_call",
                "synthetic classifier",
                (start + timedelta(milliseconds=index * 10)).isoformat(),
                (start + timedelta(milliseconds=(index + 1) * 10)).isoformat(),
                10,
                model="synthetic-model",
                input_tokens=10,
                output_tokens=2,
                token_provenance="provider",
                input_text="same synthetic input",
                output_text="same synthetic label",
                status="error" if identity == "failed" else "ok",
                error="synthetic failure" if identity == "failed" else None,
                metadata=metadata,
            )
        )
    return trace


def case(identity, family, targets, references, **options):
    summaries = {
        span: "Permission-cleared synthetic summary for " + span for span in (*references, *targets)
    }
    return SemanticInvestigation(
        identity,
        family,
        targets,
        references,
        "Preserve the independent routing-label check and all required state transitions",
        "synthetic:quality-v1",
        summaries=options.pop("summaries", summaries),
        **options,
    )


def backend(name, answer=True, confidence=None):
    return LocalCallbackJudge(
        JudgeIdentity.configured(f"synthetic.{name}", "1", {"fixture": True}),
        lambda item, **options: JudgmentAnswer(
            answer,
            usage=JudgeUsage(0, 0, 0, "reported", "reported"),
            uncertainty=JudgmentUncertainty(confidence=confidence),
        ),
    )


def main(out):
    trace = fixture_trace()
    positives = [
        case(
            "exact-reuse",
            "semantic_redundancy",
            ("duplicate",),
            ("earlier",),
            reuse_safe=True,
            reuse_contract_ref="synthetic:pure-operation-v1",
            removal_attribution_ref="synthetic:remove-duplicate-v1",
            summaries={},
        ),
        case("low-contribution", "low_contribution", ("low",), ("final",)),
        case("no-progress", "semantic_no_progress", ("step1", "step2"), ("final",)),
        case("retry", "retry_usefulness", ("retry",), ("failed",)),
        case("context", "context_relevance", ("decision",), ("context",)),
    ]
    evaluate_semantic_waste(
        trace, positives, judges=[backend("positive")], enabled=True, timeout_s=1
    )
    evaluate_semantic_waste(
        trace,
        [
            case("names-only", "semantic_redundancy", ("duplicate",), ("earlier",), summaries={}),
            case(
                "intentional-check",
                "semantic_redundancy",
                ("duplicate",),
                ("earlier",),
                retention_required=True,
            ),
        ],
        judges=[backend("unused")],
        enabled=True,
    )
    evaluate_semantic_waste(
        trace,
        [case("disagreement", "semantic_redundancy", ("duplicate",), ("earlier",))],
        judges=[backend("yes"), backend("no", False)],
        enabled=True,
    )
    evaluate_semantic_waste(
        trace,
        [case("low-confidence", "semantic_redundancy", ("duplicate",), ("earlier",))],
        judges=[backend("uncertain", confidence=0.2)],
        enabled=True,
    )

    def timeout(*args, **kwargs):
        raise TimeoutError

    failed = LocalCallbackJudge(JudgeIdentity.configured("synthetic.timeout", "1", {}), timeout)
    evaluate_semantic_waste(
        trace,
        [
            case("judge-failure", "semantic_redundancy", ("duplicate",), ("earlier",)),
            case("missing-evidence", "low_contribution", ("missing",), ("final",)),
        ],
        judges=[failed],
        enabled=True,
        timeout_s=1,
    )
    diagnosis = build_diagnosis(trace)
    semantic = [item for item in diagnosis["findings"] if item["type"] in QUESTIONS]
    assert len(semantic) == 5
    assert all(
        item["confidence"] != "high" and not item["rewrite"]["patchable"] for item in semantic
    )
    assert diagnosis["semantic_waste"]["status"] == "incomplete"
    out.mkdir(parents=True, exist_ok=True)
    trace.export_json(out / "trace.json")
    (out / "diagnosis.json").write_text(
        json.dumps(diagnosis, indent=2, allow_nan=False), encoding="utf-8"
    )
    (out / "diagnosis.md").write_text(diagnosis_to_markdown(diagnosis), encoding="utf-8")
    (out / "analysis.html").write_text(analysis_to_html(_analysis_payload(trace)), encoding="utf-8")
    print(f"Wrote five synthetic finding families and retained inconclusive cases to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/semantic-waste"))
    main(parser.parse_args().out)
