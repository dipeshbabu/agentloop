"""Synthetic email classification/priority/routing, with no mailbox actions or APIs."""

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

FIXTURES = [
    {
        "id": "support",
        "input": {
            "subject": "Password reset",
            "body": "I cannot access my account",
            "bulk": False,
            "sender": "customer",
            "verification": False,
        },
        "expected": {
            "spam": False,
            "category": "support",
            "priority": "high",
            "route": "support",
            "verified": False,
        },
    },
    {
        "id": "billing",
        "input": {
            "subject": "Invoice question",
            "body": "Please explain this invoice",
            "bulk": False,
            "sender": "customer",
            "verification": False,
        },
        "expected": {
            "spam": False,
            "category": "billing",
            "priority": "normal",
            "route": "finance",
            "verified": False,
        },
    },
    {
        "id": "newsletter",
        "input": {
            "subject": "Urgent product news",
            "body": "Our weekly release digest",
            "bulk": True,
            "sender": "known",
            "verification": False,
        },
        "expected": {
            "spam": False,
            "category": "news",
            "priority": "low",
            "route": "archive",
            "verified": False,
        },
    },
    {
        "id": "spam",
        "input": {
            "subject": "Claim a prize",
            "body": "Free gift for a random winner",
            "bulk": True,
            "sender": "unknown",
            "verification": False,
        },
        "expected": {
            "spam": True,
            "category": "spam",
            "priority": "low",
            "route": "quarantine",
            "verified": False,
        },
    },
    {
        "id": "verification",
        "input": {
            "subject": "Outage report",
            "body": "Customer service outage",
            "bulk": False,
            "sender": "customer",
            "verification": True,
        },
        "expected": {
            "spam": False,
            "category": "support",
            "priority": "high",
            "route": "support",
            "verified": True,
        },
    },
    {
        "id": "ordinary",
        "input": {
            "subject": "Meeting invitation",
            "body": "Planning notes for next week",
            "bulk": False,
            "sender": "known",
            "verification": False,
        },
        "expected": {
            "spam": False,
            "category": "other",
            "priority": "normal",
            "route": "manual_review",
            "verified": False,
        },
    },
]
VARIANTS = ("baseline", "hybrid", "cheap", "failing")


def classify(stage, payload):
    message, state = payload["message"], payload["state"]
    words = (message["subject"] + " " + message["body"]).lower()
    if stage == "spam":
        return (
            message["bulk"]
            and message["sender"] == "unknown"
            and any(word in words for word in ("prize", "free gift"))
        )
    if stage == "category":
        if state["spam"]:
            return "spam"
        if any(word in words for word in ("invoice", "charged", "refund")):
            return "billing"
        if any(word in words for word in ("password", "cannot access", "outage")):
            return "support"
        return "news" if message["bulk"] else "other"
    if stage == "priority":
        if state["spam"] or message["bulk"]:
            return "low"
        return (
            "high"
            if any(word in words for word in ("cannot access", "outage", "charged twice"))
            else "normal"
        )
    if stage == "route":
        return {
            "spam": "quarantine",
            "billing": "finance",
            "support": "support",
            "news": "archive",
            "other": "manual_review",
        }[state["category"]]
    raise ValueError("unsupported fixture stage")


def run_email(message, variant, task_id, fixture_hash):
    if variant not in VARIANTS:
        raise ValueError("unknown email configuration")
    trace = None
    try:
        with trace_workflow(
            "email-reference/" + variant,
            workflow=WorkflowInfo("reference.email-pipeline", "1.0"),
            task_id=task_id,
            metadata={
                "synthetic": True,
                "fixture_hash": fixture_hash,
                "variant": variant,
                "config_version": "1.0",
            },
        ) as trace:
            # This fixture context is intentionally irrelevant to routing and is
            # supplied only to the baseline model-style backend, never fetched.
            context = "Weather and sports fixture context unrelated to message routing. " * 8
            timestamp = utc_now_iso()
            record_operation(
                "fixture context",
                kind="transform",
                duration_ms=0,
                started_at=timestamp,
                ended_at=timestamp,
                trace=trace,
                event_id="context",
                depends_on=[],
                output_ref="sha256:" + digest(context),
                metadata={"synthetic": True},
            )
            state, previous = {}, "context"
            for stage in ("spam", "category", "priority"):
                payload = {"message": message, "state": dict(state)}
                if variant == "baseline":
                    payload["extra_context"] = context

                def predict(inputs, stage=stage):
                    if variant == "cheap" and stage == "priority":
                        # Deliberately naive: "urgent" in bulk mail is not enough
                        # to infer urgency. Independent labels catch this trap.
                        return "high" if "urgent" in message["subject"].lower() else "normal"
                    return classify(stage, inputs)

                state[stage] = decision(
                    trace,
                    stage,
                    payload,
                    predict,
                    model_fixture=variant in {"baseline", "failing"}
                    or variant == "hybrid"
                    and stage == "category",
                    depends_on=(previous,),
                    fail=variant == "failing" and task_id == "billing" and stage == "category",
                )
                previous = stage
            verification = variant == "baseline" or (
                variant in {"hybrid", "failing"} and message["verification"]
            )
            if verification:
                state["priority"] = decision(
                    trace,
                    "priority_repeat",
                    {"message": message, "state": dict(state)},
                    lambda value: classify("priority", value),
                    model_fixture=True,
                    depends_on=(previous,),
                )
                previous = "priority_repeat"
            state["verified"] = bool(message["verification"] and verification)
            state["route"] = decision(
                trace,
                "route",
                {"message": message, "state": dict(state)},
                lambda value: classify("route", value),
                model_fixture=variant in {"baseline", "failing"},
                depends_on=(previous,),
            )
            trace.metadata["output"] = state
            set_workflow_outcome(trace, "classified", output_ref="sha256:" + digest(state))
    except TimeoutError:
        # Retain the completed failure trace. The example performs no delivery,
        # deletion, forwarding or other mailbox action.
        if trace is None:
            raise
    return trace


def fixture_judge():
    def decide(request, *, timeout_s):
        # Deterministic fixture judgments over explicitly approved summaries.
        # This is not a general email/semantic model and never sees a mailbox.
        if "unrelated to the decision" in request.spec.question:
            value = "unrelated weather" in request.evidence[0].summary
        else:
            value = request.evidence[0].summary == request.evidence[1].summary
        return JudgmentAnswer(value, usage=JudgeUsage(0, 0, 0, "reported", "reported"))

    return LocalCallbackJudge(
        JudgeIdentity.configured(
            "example.email-fixture-judge",
            "1.0",
            {"fixture_rules": "1.0"},
            model_or_rule="approved-summary-fixture",
        ),
        decide,
    )


def inspect_email(trace, message, variant, *, judge=None):
    if variant != "baseline":
        return
    priority = trace.metadata["output"]["priority"]
    cases = [
        SemanticInvestigation(
            "priority-repetition",
            "semantic_redundancy",
            ("priority_repeat",),
            ("priority",),
            "Preserve all routing labels and the explicit fixture verification requirement",
            "fixture:email-labels-v1",
            summaries={
                "priority": "priority label: " + priority,
                "priority_repeat": "priority label: " + priority,
            },
            retention_required=message["verification"],
        ),
        SemanticInvestigation(
            "context-relevance",
            "context_relevance",
            ("category",),
            ("context",),
            "Classify message category from message content and sender/bulk fields",
            "fixture:email-labels-v1",
            summaries={
                "context": "unrelated weather and sports fixture text",
                "category": "category decision uses message and sender fields",
            },
        ),
    ]
    evaluate_semantic_waste(
        trace, cases, judges=[judge or fixture_judge()], enabled=True, timeout_s=1
    )


def main(out, *, judge=None):
    result = write_reference_bundle(
        out,
        name="Email pipeline reference",
        fixtures=FIXTURES,
        variants=VARIANTS,
        run_case=run_email,
        inspect_trace=lambda trace, message, variant: inspect_email(
            trace, message, variant, judge=judge
        ),
    )
    hybrid = [item for item in result["comparisons"] if item["variant"] == "hybrid"]
    cheap = [item for item in result["comparisons"] if item["variant"] == "cheap"]
    assert all(item["candidate_quality"] == 1 for item in hybrid)
    assert any(item["candidate_quality"] < 1 for item in cheap)
    assert any(
        item["candidate_quality"] is None
        for item in result["comparisons"]
        if item["variant"] == "failing"
    )
    print(f"Wrote executed synthetic email pipelines and replay/study evidence to {out}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/email-reference"))
    main(parser.parse_args().out)
