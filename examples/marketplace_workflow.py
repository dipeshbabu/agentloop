"""Synthetic listing decisions and review labels; no moderation actions or service."""

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
    set_workflow_outcome,
    trace_workflow,
)

if __package__:
    from .reference_support import decision, digest, write_reference_bundle
else:
    from reference_support import decision, digest, write_reference_bundle


def labels(duplicate, moderation, category, route, verified=False):
    return {
        "duplicate": duplicate,
        "moderation": moderation,
        "category": category,
        "route": route,
        "verified": verified,
    }


FIXTURES = [
    {
        "id": "ordinary",
        "input": {
            "title": "plain fixture shirt",
            "match_title": "other fixture shirt",
            "identity": "new-shirt",
            "match_identity": "other-shirt",
            "policy_flag": False,
            "category_hint": "apparel",
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(False, "clear", "apparel", "listing_review"),
    },
    {
        "id": "exact-duplicate",
        "input": {
            "title": "canvas fixture bag",
            "match_title": "canvas fixture bag",
            "identity": "same-bag",
            "match_identity": "same-bag",
            "policy_flag": False,
            "category_hint": "accessories",
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(True, "clear", "accessories", "duplicate_review"),
    },
    {
        "id": "near-variant",
        "input": {
            "title": "cotton fixture shirt",
            "match_title": "cotton fixture shirt",
            "identity": "blue-medium",
            "match_identity": "red-medium",
            "policy_flag": False,
            "category_hint": "apparel",
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(False, "clear", "apparel", "listing_review"),
    },
    {
        "id": "benign-keyword",
        "input": {
            "title": "flag patterned fixture shirt",
            "match_title": "plain fixture shirt",
            "identity": "pattern-shirt",
            "match_identity": "plain-shirt",
            "policy_flag": False,
            "category_hint": "apparel",
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(False, "clear", "apparel", "listing_review"),
    },
    {
        "id": "policy-flag",
        "input": {
            "title": "generic fixture accessory",
            "match_title": "other fixture accessory",
            "identity": "new-accessory",
            "match_identity": "other-accessory",
            "policy_flag": True,
            "category_hint": "accessories",
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(False, "flagged", "accessories", "policy_review"),
    },
    {
        "id": "category-ambiguity",
        "input": {
            "title": "assorted fixture lot",
            "match_title": "other fixture lot",
            "identity": "mixed-lot",
            "match_identity": "other-lot",
            "policy_flag": False,
            "category_hint": None,
            "evidence_complete": True,
            "verification": False,
        },
        "expected": labels(False, "clear", None, "manual_review"),
    },
    {
        "id": "missing-evidence",
        "input": {
            "title": "unverified fixture item",
            "match_title": "unverified fixture item",
            "identity": None,
            "match_identity": None,
            "policy_flag": None,
            "category_hint": None,
            "evidence_complete": False,
            "verification": False,
        },
        "expected": labels(None, "unknown", None, "manual_review"),
    },
    {
        "id": "required-verification",
        "input": {
            "title": "fixture lamp",
            "match_title": "different fixture lamp",
            "identity": "lamp-a",
            "match_identity": "lamp-b",
            "policy_flag": False,
            "category_hint": "home",
            "evidence_complete": True,
            "verification": True,
        },
        "expected": labels(False, "clear", "home", "listing_review", True),
    },
]
VARIANTS = ("baseline", "hybrid", "cheap", "failing")


def decide(stage, payload, *, cheap=False):
    item, state = payload["item"], payload["state"]
    if stage == "duplicate":
        if cheap:
            return item["title"].casefold() == item["match_title"].casefold()
        if (
            not item["evidence_complete"]
            or item["identity"] is None
            or item["match_identity"] is None
        ):
            return None
        return item["identity"] == item["match_identity"]
    if stage == "moderation":
        if cheap:
            return "flagged" if "flag" in item["title"].lower() else "clear"
        if not item["evidence_complete"] or item["policy_flag"] is None:
            return "unknown"
        return "flagged" if item["policy_flag"] else "clear"
    if stage == "category":
        if cheap:
            return item["category_hint"] or "apparel"
        return item["category_hint"] if item["evidence_complete"] else None
    if stage == "route":
        if (
            state["moderation"] == "unknown"
            or state["duplicate"] is None
            or state["category"] is None
        ):
            return "manual_review"
        if state["moderation"] == "flagged":
            return "policy_review"
        return "duplicate_review" if state["duplicate"] else "listing_review"
    raise ValueError("unsupported fixture stage")


def run_listing(item, variant, task_id, fixture_hash):
    if variant not in VARIANTS:
        raise ValueError("unknown marketplace configuration")
    trace = None
    try:
        with trace_workflow(
            "marketplace-reference/" + variant,
            workflow=WorkflowInfo("reference.marketplace-processing", "1.0"),
            task_id=task_id,
            metadata={
                "synthetic": True,
                "fixture_hash": fixture_hash,
                "variant": variant,
                "config_version": "1.0",
                "action_mode": "review_labels_only",
            },
        ) as trace:
            state = {}
            state["duplicate"] = decision(
                trace,
                "duplicate",
                {"item": item, "state": {}},
                lambda value: decide("duplicate", value, cheap=variant == "cheap"),
                model_fixture=variant in {"baseline", "failing"},
            )
            previous = "duplicate"
            verify = variant == "baseline" or variant != "cheap" and item["verification"]
            if verify:
                state["duplicate"] = decision(
                    trace,
                    "identity_recheck",
                    {"item": item, "state": dict(state)},
                    lambda value: decide("duplicate", value),
                    model_fixture=True,
                    depends_on=(previous,),
                )
                previous = "identity_recheck"
            state["verified"] = bool(item["verification"] and verify)
            for stage in ("moderation", "category", "route"):
                state[stage] = decision(
                    trace,
                    stage,
                    {"item": item, "state": dict(state)},
                    lambda value, stage=stage: decide(stage, value, cheap=variant == "cheap"),
                    model_fixture=variant in {"baseline", "failing"}
                    or variant == "hybrid"
                    and stage == "moderation",
                    depends_on=(previous,),
                    fail=variant == "failing" and task_id == "ordinary" and stage == "category",
                )
                previous = stage
            trace.metadata["output"] = state
            set_workflow_outcome(
                trace, "review_labels_recorded", output_ref="sha256:" + digest(state)
            )
    except TimeoutError:
        if trace is None:
            raise
    return trace


def inspect_listing(trace, item, variant, *, judge=None):
    if variant != "baseline":
        return
    if judge is None:
        judge = LocalCallbackJudge(
            JudgeIdentity.configured(
                "example.marketplace-fixture-judge",
                "1.0",
                {"fixture_rules": "1.0"},
                model_or_rule="approved-summary-equality",
            ),
            lambda request, **options: JudgmentAnswer(
                request.evidence[0].summary == request.evidence[1].summary,
                usage=JudgeUsage(0, 0, 0, "reported", "reported"),
            ),
        )
    duplicate = trace.metadata["output"]["duplicate"]
    case = SemanticInvestigation(
        "overlapping-identity-check",
        "semantic_redundancy",
        ("identity_recheck",),
        ("duplicate",),
        "Preserve labelled deduplication/moderation/routing outcomes and required verification; no production moderation action",
        "fixture:marketplace-labels-v1",
        summaries={
            "duplicate": "identity comparison: " + str(duplicate),
            "identity_recheck": "identity comparison: " + str(duplicate),
        },
        retention_required=item["verification"],
    )
    evaluate_semantic_waste(trace, [case], judges=[judge], enabled=True, timeout_s=1)


def main(out, *, judge=None):
    report = write_reference_bundle(
        out,
        name="Marketplace processing reference",
        fixtures=FIXTURES,
        variants=VARIANTS,
        run_case=run_listing,
        inspect_trace=lambda trace, item, variant: inspect_listing(
            trace, item, variant, judge=judge
        ),
        field_diagnostics=True,
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
    print(f"Wrote synthetic marketplace comparisons to {out}; no listing action was performed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/marketplace-reference"))
    main(parser.parse_args().out)
