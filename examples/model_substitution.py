"""Synthetic offline replacements; no providers, model downloads or live routing."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop import AgentTrace, JudgeIdentity, JudgeUsage, record_operation
from agentloop.substitution_export import export_substitution_studies
from agentloop.substitution_types import (
    DecisionImplementation,
    DecisionResult,
    DecisionStep,
    SubstitutionExample,
    SubstitutionProtocol,
)
from agentloop.substitutions import run_substitution_experiment


def baseline(request, *, timeout_s):
    message = request.inputs["text"]
    label = "billing" if "invoice" in message else "support"
    return DecisionResult(
        label,
        usage=JudgeUsage(100, 5, 0.02, "estimated", "calculated"),
        cost_ref="synthetic:baseline-cost-v1",
    )


def keyword_rule(request, *, timeout_s):
    message = request.inputs["text"]
    if "invoice" in message or "billing" in message:
        label = "billing"
    elif any(word in message for word in ("password", "start", "problem")):
        label = "support"
    else:
        label = "other"
    return DecisionResult(
        label,
        usage=JudgeUsage(0, 0, 0, "reported", "calculated"),
        cost_ref="synthetic:rule-cost-v1",
    )


def baseline_copy(request, *, timeout_s):
    result = baseline(request, timeout_s=timeout_s)
    return DecisionResult(
        result.output,
        usage=JudgeUsage(0, 0, 0, "reported", "calculated"),
        cost_ref="synthetic:copy-cost-v1",
    )


def fragile_rule(request, *, timeout_s):
    if request.inputs["text"] == "malformed fixture":
        raise TimeoutError
    return keyword_rule(request, timeout_s=timeout_s)


def implementation(name, invoke, kind):
    return DecisionImplementation(
        name,
        JudgeIdentity.configured(
            "example." + name,
            "1",
            {"fixture_revision": "1"},
            provider="synthetic-local",
            model_or_rule=name,
        ),
        invoke,
        kind,
    )


def main(out):
    source = AgentTrace(
        "synthetic source workflow",
        run_id="substitution-source",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00.010000+00:00",
        elapsed_ms=10,
        metadata={"synthetic": True},
    )
    record_operation(
        "routing",
        kind="classifier",
        duration_ms=10,
        started_at=source.started_at,
        ended_at=source.ended_at,
        trace=source,
        event_id="routing",
    )
    step = DecisionStep.from_trace(source, "routing", step_id="routing", version="1")
    fixtures = [
        ("password reset request", "support"),
        ("invoice reminder", "billing"),
        ("weekly release digest", "other"),
        ("billing invoice question", "billing"),
        ("product will not start", "support"),
        ("summer promotion", "other"),
        ("malformed fixture", "other"),
    ]
    examples = [
        SubstitutionExample(
            str(index),
            {"text": value},
            input_ref=f"synthetic:routing-input:{index}",
            quality_ref=f"synthetic:routing-label-v1:{index}",
            expected=label,
        )
        for index, (value, label) in enumerate(fixtures)
    ]
    protocol = SubstitutionProtocol(
        "routing implementation comparison",
        "1",
        step,
        examples,
        {"type": "decision", "version": "1.0", "labels": ["support", "billing", "other"]},
        seed=7,
        timeout_s=1,
    )
    result = run_substitution_experiment(
        protocol,
        implementation("baseline-model-fixture", baseline, "model"),
        [
            implementation("keyword-rule", keyword_rule, "rule"),
            implementation("baseline-copy", baseline_copy, "rule"),
            implementation("fragile-rule", fragile_rule, "rule"),
        ],
        enabled=True,
    )
    exported = export_substitution_studies(result, out)
    comparisons = {item["condition"]: item for item in exported["comparison"]["comparisons"]}
    assert comparisons["keyword-rule"]["quality_preserved_on_examples"] is True
    assert comparisons["baseline-copy"]["baseline_agreement"]["observed_rate"] == 1
    assert comparisons["baseline-copy"]["quality_preserved_on_examples"] is False
    assert comparisons["fragile-rule"]["quality_preserved_on_examples"] is None
    print(f"Wrote three synthetic candidate comparisons and native paired studies to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/model-substitution"))
    main(parser.parse_args().out)
