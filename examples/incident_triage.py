"""Offline incident triage proposals; no credentials, commands or infrastructure actions."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop import (
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    LocalCallbackJudge,
    SemanticInvestigation,
    WorkflowInfo,
    evaluate_semantic_waste,
    record_operation,
    set_workflow_outcome,
    trace_workflow,
)
from agentloop.events import utc_now_iso

if __package__:
    from .reference_support import decision, digest, write_reference_bundle
else:
    from reference_support import decision, digest, write_reference_bundle


def expected(anomaly, severity, proposal, rollback, escalate, verified=False):
    return {
        "anomaly": anomaly,
        "severity": severity,
        "response": {"proposal": proposal, "execute": False},
        "disposition": {
            "rollback_proposed": rollback,
            "escalate": escalate,
            "execution_permitted": False,
        },
        "verified": verified,
    }


FIXTURES = [
    {
        "id": "normal",
        "input": {
            "sustained_error": False,
            "latency_spike": False,
            "noise_only": False,
            "evidence_complete": True,
            "critical_dependency": False,
            "recent_change": False,
            "rollback_candidate_documented": False,
            "verification": False,
        },
        "expected": expected(False, "normal", "observe", False, False),
    },
    {
        "id": "noisy",
        "input": {
            "sustained_error": False,
            "latency_spike": True,
            "noise_only": True,
            "evidence_complete": True,
            "critical_dependency": False,
            "recent_change": False,
            "rollback_candidate_documented": False,
            "verification": False,
        },
        "expected": expected(False, "normal", "observe", False, False),
    },
    {
        "id": "ambiguous",
        "input": {
            "sustained_error": True,
            "latency_spike": True,
            "noise_only": False,
            "evidence_complete": False,
            "critical_dependency": True,
            "recent_change": True,
            "rollback_candidate_documented": False,
            "verification": False,
        },
        "expected": expected(None, "unknown", "gather_evidence", None, True),
    },
    {
        "id": "critical-error",
        "input": {
            "sustained_error": True,
            "latency_spike": False,
            "noise_only": False,
            "evidence_complete": True,
            "critical_dependency": True,
            "recent_change": True,
            "rollback_candidate_documented": True,
            "verification": True,
        },
        "expected": expected(True, "critical", "review_rollback", True, True, True),
    },
    {
        "id": "warning",
        "input": {
            "sustained_error": False,
            "latency_spike": True,
            "noise_only": False,
            "evidence_complete": True,
            "critical_dependency": False,
            "recent_change": False,
            "rollback_candidate_documented": False,
            "verification": False,
        },
        "expected": expected(True, "warning", "investigate", False, False),
    },
    {
        "id": "critical-no-rollback",
        "input": {
            "sustained_error": True,
            "latency_spike": False,
            "noise_only": False,
            "evidence_complete": True,
            "critical_dependency": True,
            "recent_change": False,
            "rollback_candidate_documented": False,
            "verification": True,
        },
        "expected": expected(True, "critical", "escalate", False, True, True),
    },
    {
        "id": "recovered",
        "input": {
            "sustained_error": False,
            "latency_spike": False,
            "noise_only": False,
            "evidence_complete": True,
            "critical_dependency": False,
            "recent_change": True,
            "rollback_candidate_documented": True,
            "verification": False,
        },
        "expected": expected(False, "normal", "observe", False, False),
    },
]
VARIANTS = ("baseline", "hybrid", "cheap", "failing")


def decide(stage, payload, *, cheap=False):
    signals, state = payload["signals"], payload["state"]
    if stage == "anomaly":
        if cheap:
            return bool(signals["latency_spike"])
        if not signals["evidence_complete"]:
            return None
        return bool(
            signals["sustained_error"] or signals["latency_spike"] and not signals["noise_only"]
        )
    if stage == "severity":
        if state["anomaly"] is None:
            return "unknown"
        if not state["anomaly"]:
            return "normal"
        return "critical" if cheap or signals["critical_dependency"] else "warning"
    if stage == "response":
        severity = state["severity"]
        if severity == "unknown":
            proposal = "gather_evidence"
        elif severity == "normal":
            proposal = "observe"
        elif severity == "critical":
            proposal = (
                "review_rollback"
                if signals["recent_change"] and signals["rollback_candidate_documented"]
                else "escalate"
            )
        else:
            proposal = "investigate"
        return {"proposal": proposal, "execute": False}
    if stage == "disposition":
        proposal = state["response"]["proposal"]
        return {
            "rollback_proposed": None
            if state["severity"] == "unknown"
            else proposal == "review_rollback",
            "escalate": state["severity"] in {"critical", "unknown"},
            "execution_permitted": False,
        }
    raise ValueError("unsupported fixture stage")


def run_incident(signals, variant, task_id, fixture_hash):
    if variant not in VARIANTS:
        raise ValueError("unknown incident configuration")
    trace = None
    try:
        with trace_workflow(
            "incident-reference/" + variant,
            workflow=WorkflowInfo("reference.incident-triage", "1.0"),
            task_id=task_id,
            metadata={
                "synthetic": True,
                "fixture_hash": fixture_hash,
                "variant": variant,
                "config_version": "1.0",
                "action_mode": "inert_proposals_only",
            },
        ) as trace:
            context = "Old unrelated change notes for a different synthetic service. " * 8
            timestamp = utc_now_iso()
            record_operation(
                "fixture context",
                kind="transform",
                duration_ms=0,
                started_at=timestamp,
                ended_at=timestamp,
                depends_on=[],
                trace=trace,
                event_id="context",
                output_ref="sha256:" + digest(context),
                metadata={"synthetic": True},
            )
            state, previous = {}, "context"
            for stage in ("anomaly", "severity"):
                payload = {"signals": signals, "state": dict(state)}
                if variant == "baseline":
                    payload["extra_context"] = context
                state[stage] = decision(
                    trace,
                    stage,
                    payload,
                    lambda value, stage=stage: decide(stage, value, cheap=variant == "cheap"),
                    model_fixture=variant in {"baseline", "failing"}
                    or variant == "hybrid"
                    and stage == "anomaly",
                    depends_on=(previous,),
                )
                previous = stage
            verify = variant == "baseline" or variant != "cheap" and signals["verification"]
            if verify:
                state["severity"] = decision(
                    trace,
                    "severity_repeat",
                    {"signals": signals, "state": dict(state)},
                    lambda value: decide("severity", value),
                    model_fixture=True,
                    depends_on=(previous,),
                )
                previous = "severity_repeat"
            state["verified"] = bool(signals["verification"] and verify)
            for stage in ("response", "disposition"):
                state[stage] = decision(
                    trace,
                    stage,
                    {"signals": signals, "state": dict(state)},
                    lambda value, stage=stage: decide(stage, value),
                    model_fixture=variant in {"baseline", "failing"},
                    depends_on=(previous,),
                    fail=variant == "failing"
                    and task_id == "critical-error"
                    and stage == "response",
                )
                previous = stage
            trace.metadata["output"] = state
            set_workflow_outcome(trace, "proposal_recorded", output_ref="sha256:" + digest(state))
    except TimeoutError:
        if trace is None:
            raise
    return trace


def fixture_judge():
    def assess(request, *, timeout_s):
        value = (
            "unrelated service" in request.evidence[0].summary
            if "unrelated to the decision" in request.spec.question
            else request.evidence[0].summary == request.evidence[1].summary
        )
        return JudgmentAnswer(value, usage=JudgeUsage(0, 0, 0, "reported", "reported"))

    return LocalCallbackJudge(
        JudgeIdentity.configured(
            "example.incident-fixture-judge",
            "1.0",
            {"fixture_rules": "1.0"},
            model_or_rule="approved-summary-fixture",
        ),
        assess,
    )


def inspect_incident(trace, signals, variant, *, judge=None):
    if variant != "baseline":
        return
    severity = trace.metadata["output"]["severity"]
    cases = [
        SemanticInvestigation(
            "repeated-severity",
            "semantic_redundancy",
            ("severity_repeat",),
            ("severity",),
            "Preserve frozen triage labels and explicitly required verification; proposals never authorize execution",
            "fixture:incident-labels-v1",
            summaries={
                "severity": "severity: " + severity,
                "severity_repeat": "severity: " + severity,
            },
            retention_required=signals["verification"],
        ),
        SemanticInvestigation(
            "context-relevance",
            "context_relevance",
            ("severity",),
            ("context",),
            "Triage this fixture's supplied signals; no operational safety judgment",
            "fixture:incident-labels-v1",
            summaries={
                "context": "unrelated service change notes",
                "severity": "severity uses current incident evidence",
            },
        ),
    ]
    evaluate_semantic_waste(
        trace, cases, judges=[judge or fixture_judge()], enabled=True, timeout_s=1
    )


def main(out, *, judge=None):
    report = write_reference_bundle(
        out,
        name="Incident triage reference",
        fixtures=FIXTURES,
        variants=VARIANTS,
        run_case=run_incident,
        inspect_trace=lambda trace, signals, variant: inspect_incident(
            trace, signals, variant, judge=judge
        ),
    )
    assert all(
        item["candidate_quality"] == 1
        for item in report["comparisons"]
        if item["variant"] == "hybrid"
    )
    assert any(
        item["candidate_quality"] < 1
        for item in report["comparisons"]
        if item["variant"] == "cheap"
    )
    assert any(
        item["candidate_quality"] is None
        for item in report["comparisons"]
        if item["variant"] == "failing"
    )
    print(f"Wrote synthetic incident comparisons to {out}; no infrastructure action was executed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/incident-reference"))
    main(parser.parse_args().out)
