from __future__ import annotations

from dataclasses import replace

import pytest

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgmentAnswer,
    JudgmentUncertainty,
    LocalCallbackJudge,
)
from agentloop.events import AgentEvent
from agentloop.judgment_types import JUDGMENT_KEY, fingerprint
from agentloop.semantic_waste import evaluate_semantic_waste, read_semantic_waste
from agentloop.semantic_waste_types import QUESTIONS, SEMANTIC_WASTE_KEY, SemanticInvestigation


def trace_fixture():
    trace = AgentTrace(
        "synthetic semantic investigation",
        run_id="semantic-run",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00.030000+00:00",
        elapsed_ms=30,
        metadata={"synthetic": True},
    )
    for index, identity in enumerate(("reference", "target", "downstream")):
        trace.events.append(
            AgentEvent(
                identity,
                trace.run_id,
                "model_call",
                "same operation",
                f"2026-01-01T00:00:00.{index * 10000:06d}+00:00",
                f"2026-01-01T00:00:00.{(index + 1) * 10000:06d}+00:00",
                10,
                model="test-model",
                input_tokens=10,
                output_tokens=2,
                token_provenance="provider",
                input_text="same complete input",
                output_text="same recorded output",
                metadata={"provider_reported_cost_usd": 0.001},
            )
        )
    return trace


def investigation(family="semantic_redundancy", **changes):
    targets = ("target", "downstream") if family == "semantic_no_progress" else ("target",)
    reference = "downstream" if family == "low_contribution" else "reference"
    summaries = {
        reference: "approved reference result",
        **{key: "approved target result" for key in targets},
    }
    return replace(
        SemanticInvestigation(
            "case-1",
            family,
            targets,
            (reference,),
            "Preserve the independently checked final routing label",
            "quality:route-v1",
            summaries=summaries,
        ),
        **changes,
    )


def judge(answer=JudgmentAnswer(True), name="fixture"):
    return LocalCallbackJudge(
        JudgeIdentity.configured(name, "1", {}), lambda item, **options: answer
    )


@pytest.mark.parametrize("family", list(QUESTIONS))
def test_each_family_accepts_purpose_bound_known_evidence_without_high_confidence(family):
    trace = trace_fixture()
    if family == "retry_usefulness":
        trace.events[1].metadata["retry_of"] = "reference"
    result = evaluate_semantic_waste(trace, [investigation(family)], judges=[judge()], enabled=True)
    assert result["cases"][0]["status"] == "supported"
    assert result["cases"][0]["confidence"] == "low"
    assert result["cases"][0]["judgments"][0]["judge"]["implementation"] == "fixture"
    assert read_semantic_waste(trace) == result


def test_deterministic_exact_reuse_preferred_only_under_explicit_host_contract():
    trace = trace_fixture()
    calls = []
    backend = LocalCallbackJudge(
        JudgeIdentity.configured("spy", "1", {}), lambda *args, **kwargs: calls.append(args)
    )
    case = investigation(
        reuse_safe=True, reuse_contract_ref="host:side-effect-free-complete-inputs-v1", summaries={}
    )
    result = evaluate_semantic_waste(trace, [case], judges=[backend], enabled=True)
    assert result["cases"][0]["basis"] == "deterministic_reuse"
    assert result["cases"][0]["status"] == "supported"
    assert calls == []


@pytest.mark.parametrize(
    "trap",
    [
        "names_only",
        "changed_input",
        "changed_output",
        "changed_context",
        "changed_usage",
        "retention",
        "unordered",
    ],
)
def test_false_positive_traps_do_not_become_deterministic_redundancy(trap):
    trace = trace_fixture()
    case = investigation(reuse_safe=True, reuse_contract_ref="host:reuse-v1", summaries={})
    if trap == "names_only":
        case = replace(case, reuse_safe=False)
    elif trap == "changed_input":
        trace.events[1].input_text = "different required data"
    elif trap == "changed_output":
        trace.events[1].output_text = "new result"
    elif trap == "changed_context":
        trace.events[1].metadata["model_revision"] = "different implementation"
    elif trap == "changed_usage":
        trace.events[1].input_tokens += 1
    elif trap == "retention":
        case = replace(case, retention_required=True)
    else:
        trace.events[1].started_at = trace.events[0].started_at
        trace.events[1].ended_at = trace.events[0].ended_at
    result = evaluate_semantic_waste(trace, [case])
    assert result["cases"][0]["status"] in {"unknown", "retained"}


def test_missing_approved_context_never_triggers_a_judge():
    calls = []
    backend = LocalCallbackJudge(
        JudgeIdentity.configured("spy", "1", {}), lambda *args, **kwargs: calls.append(args)
    )
    result = evaluate_semantic_waste(
        trace_fixture(), [investigation(summaries={})], judges=[backend], enabled=True
    )
    assert result["cases"][0]["reason"] == "missing_approved_summaries"
    assert calls == []


def test_default_disabled_does_not_call_judge():
    calls = []
    backend = LocalCallbackJudge(
        JudgeIdentity.configured("spy", "1", {}), lambda *args, **kwargs: calls.append(args)
    )
    result = evaluate_semantic_waste(trace_fixture(), [investigation()], judges=[backend])
    assert result["cases"][0]["status"] == "unknown"
    assert result["cases"][0]["judgments"][0]["effective_status"] == "disabled"
    assert calls == []


def test_disagreement_low_confidence_and_negative_answers_are_not_findings():
    result = evaluate_semantic_waste(
        trace_fixture(),
        [investigation()],
        judges=[judge(), judge(JudgmentAnswer(False), "contrary")],
        enabled=True,
    )
    assert result["cases"][0]["status"] == "disagreement"
    uncertain = JudgmentAnswer(True, uncertainty=JudgmentUncertainty(confidence=0.2))
    result = evaluate_semantic_waste(
        trace_fixture(), [investigation()], judges=[judge(uncertain)], enabled=True
    )
    assert result["cases"][0]["status"] == "low_confidence"
    result = evaluate_semantic_waste(
        trace_fixture(), [investigation()], judges=[judge(JudgmentAnswer(False))], enabled=True
    )
    assert result["cases"][0]["status"] == "not_supported"


def test_judge_failure_missing_source_and_stale_source_are_explicit():
    def failure(*args, **kwargs):
        raise TimeoutError("private")

    backend = LocalCallbackJudge(JudgeIdentity.configured("timeout", "1", {}), failure)
    result = evaluate_semantic_waste(
        trace_fixture(), [investigation()], judges=[backend], enabled=True
    )
    assert result["cases"][0]["status"] == "unknown"
    assert "private" not in str(result)
    trace = trace_fixture()
    trace.events.pop(1)
    result = evaluate_semantic_waste(trace, [investigation()], judges=[judge()], enabled=True)
    assert result["cases"][0]["reason"] == "missing_or_invalid_evidence"
    trace = trace_fixture()
    evaluate_semantic_waste(trace, [investigation()], judges=[judge()], enabled=True)
    trace.events[1].output_text = "changed"
    assert read_semantic_waste(trace)["cases"][0]["status"] == "stale"


def test_investigation_ids_cannot_silently_replace_prior_evidence():
    trace = trace_fixture()
    evaluate_semantic_waste(trace, [investigation()], judges=[judge()], enabled=True)
    with pytest.raises(ValueError, match="new investigation ID"):
        evaluate_semantic_waste(trace, [investigation()], judges=[judge()], enabled=True)


def test_host_metadata_is_preserved_and_read_never_executes(monkeypatch):
    trace = trace_fixture()
    trace.metadata["host"] = {"keep": True}
    evaluate_semantic_waste(trace, [investigation()], judges=[judge()], enabled=True)
    monkeypatch.setattr(
        LocalCallbackJudge, "judge", lambda *args, **kwargs: pytest.fail("no execution during read")
    )
    assert read_semantic_waste(trace)["cases"][0]["status"] == "supported"
    assert trace.metadata["host"] == {"keep": True}


@pytest.mark.parametrize(
    "family,reason",
    [
        ("retry_usefulness", "missing_retry_relation"),
        ("low_contribution", "missing_downstream_evidence"),
        ("semantic_no_progress", "unordered_sequence"),
        ("context_relevance", "ineligible_context_target"),
    ],
)
def test_family_specific_evidence_gaps_do_not_call_judges(family, reason):
    trace = trace_fixture()
    case = investigation(family)
    if family == "low_contribution":
        case = replace(
            case,
            reference_spans=("reference",),
            summaries={"reference": "prior result", "target": "work"},
        )
    elif family == "semantic_no_progress":
        case = replace(case, target_spans=tuple(reversed(case.target_spans)))
    elif family == "context_relevance":
        trace.events[1].event_type = "tool_call"
    calls = []
    backend = LocalCallbackJudge(
        JudgeIdentity.configured("spy", "1", {}), lambda *args, **kwargs: calls.append(args)
    )
    result = evaluate_semantic_waste(trace, [case], judges=[backend], enabled=True)
    assert result["cases"][0]["status"] == "unknown"
    assert result["cases"][0]["reason"] == reason
    assert calls == []


def test_changed_criteria_reference_cannot_reuse_an_incompatible_cached_opinion():
    trace = trace_fixture()
    calls = []

    def answer(request, **options):
        calls.append(request.spec.question)
        return JudgmentAnswer(True)

    backend = LocalCallbackJudge(JudgeIdentity.configured("spy", "1", {}), answer)
    cases = [
        investigation(),
        investigation(investigation_id="case-2", criteria_ref="quality:different-version"),
    ]
    evaluate_semantic_waste(trace, cases, judges=[backend], enabled=True)
    assert len(calls) == 2 and calls[0] != calls[1]


def test_source_mutation_cannot_commit_partially_bound_evidence():
    trace = trace_fixture()

    def mutate(request, **options):
        trace.events[1].output_text = "changed during evaluation"
        return JudgmentAnswer(True)

    backend = LocalCallbackJudge(JudgeIdentity.configured("mutator", "1", {}), mutate)
    with pytest.raises(ValueError, match="does not match"):
        evaluate_semantic_waste(trace, [investigation()], judges=[backend], enabled=True)
    assert SEMANTIC_WASTE_KEY not in trace.metadata and JUDGMENT_KEY not in trace.metadata


def test_unrelated_receipt_and_altered_declaration_do_not_support_findings():
    trace = trace_fixture()
    evaluate_semantic_waste(
        trace,
        [
            investigation(),
            investigation(
                investigation_id="other", task_criteria="Different independently checked task"
            ),
        ],
        judges=[judge()],
        enabled=True,
    )
    envelope = trace.metadata[SEMANTIC_WASTE_KEY]
    envelope["cases"][0]["judgment_hashes"] = envelope["cases"][1]["judgment_hashes"]
    envelope["evidence_hash"] = fingerprint(
        {key: value for key, value in envelope.items() if key != "evidence_hash"}
    )
    assert read_semantic_waste(trace)["cases"][0]["reason"] == "missing_or_mismatched_judgment"
    envelope["cases"][1]["definition"]["task_criteria"] = "tampered criteria"
    assert read_semantic_waste(trace)["status"] == "invalid"


def test_malformed_or_unsupported_namespace_does_not_execute():
    trace = trace_fixture()
    for value in (None, [], {}, {"schema_version": "1.0"}, {"schema_version": "9.0"}):
        trace.metadata[SEMANTIC_WASTE_KEY] = value
        assert read_semantic_waste(trace)["status"] in {"invalid", "unsupported"}
