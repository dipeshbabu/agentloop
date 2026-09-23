from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from workflow_fixtures import pipeline

from agentloop import (
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSession,
    JudgmentSpec,
    JudgmentUncertainty,
    LocalCallbackJudge,
    attach_judgment,
    judgment_request,
    read_judgments,
)
from agentloop.judgment_types import JUDGMENT_KEY, canonical, fingerprint
from agentloop.judgments import validate_judgment_record


def identity(**kwargs):
    return JudgeIdentity.configured("local.test", "1", {"mode": "test"}, **kwargs)


def request(trace=None, spec=None):
    return judgment_request(
        trace or pipeline(), spec or JudgmentSpec("Same work?", "boolean"), ["classify", "priority"]
    )


def session(answer=JudgmentAnswer(True), **kwargs):
    return JudgmentSession(LocalCallbackJudge(identity(), lambda item, **options: answer), **kwargs)


@pytest.mark.parametrize(
    "spec,value",
    [
        (JudgmentSpec("Same?", "boolean"), False),
        (JudgmentSpec("Likelihood?", "probability"), 0.75),
        (JudgmentSpec("Which?", "choice", ["same", "different"]), "same"),
        (JudgmentSpec("How useful?", "score", minimum=-2, maximum=2), -2),
    ],
)
def test_typed_results_are_explicit_and_roundtrip(spec, value):
    result = session(JudgmentAnswer(value)).evaluate(request(spec=spec), enabled=True)
    assert result["evaluation"]["status"] == "known"
    assert type(result["evaluation"]["value"]) is type(value)
    assert result["evaluation"]["value"] == value
    assert validate_judgment_record(result) == result
    assert result["evaluation"]["uncertainty"]["calibration_status"] == "uncalibrated"


@pytest.mark.parametrize(
    "spec,value",
    [
        (JudgmentSpec("Same?", "boolean"), 1),
        (JudgmentSpec("Likelihood?", "probability"), True),
        (JudgmentSpec("Likelihood?", "probability"), 1.01),
        (JudgmentSpec("Which?", "choice", ["same", "different"]), "other"),
        (JudgmentSpec("How useful?", "score", minimum=-2, maximum=2), -2.01),
    ],
)
def test_invalid_values_become_unknown_not_coerced(spec, value):
    result = session(JudgmentAnswer(value)).evaluate(request(spec=spec), enabled=True)
    assert result["evaluation"]["status"] == "invalid_result"
    assert result["evaluation"]["value"] is None
    validate_judgment_record(result)


def test_opt_in_cannot_be_overridden_by_cached_answer():
    calls = []

    def judge(item, **options):
        calls.append(item)
        return JudgmentAnswer(False)

    evaluator = JudgmentSession(LocalCallbackJudge(identity(), judge))
    assert evaluator.evaluate(request())["evaluation"]["status"] == "disabled"
    assert calls == []
    evaluator.evaluate(request(), enabled=True)
    result = evaluator.evaluate(request())
    assert result["evaluation"]["status"] == "disabled" and len(calls) == 1
    assert not result["invocation"]["cache_hit"]


def test_evidence_is_referenced_and_raw_content_never_forwarded():
    trace = pipeline()
    event = trace.events[0]
    event.name = event.input_text = event.output_text = event.error = "private-secret"
    event.metadata["private"] = "private-secret"
    event.metadata["operation_kind"] = "private-secret"
    item = request(trace)
    assert "private-secret" not in canonical(item.to_dict())
    assert item.evidence[0].operation_kind == "unknown"
    old_hash = item.evidence[0].source_hash
    event.output_text = "different secret"
    assert request(trace).evidence[0].source_hash != old_hash
    explicit = judgment_request(
        trace, item.spec, ["classify"], summaries={"classify": "approved derived summary"}
    )
    assert explicit.evidence[0].summary == "approved derived summary"


@pytest.mark.parametrize(
    "error,status", [(TimeoutError("secret"), "timeout"), (RuntimeError("secret"), "error")]
)
def test_failures_are_unavailable_private_and_not_cached(error, status):
    calls = []

    def judge(item, **options):
        calls.append(item)
        raise error

    evaluator = JudgmentSession(LocalCallbackJudge(identity(), judge))
    for _ in range(2):
        result = evaluator.evaluate(request(), enabled=True)
        assert result["evaluation"]["status"] == status
        assert result["evaluation"]["value"] is None
        assert result["invocation"]["usage"]["cost_usd"] is None
        assert "secret" not in canonical(result)
        validate_judgment_record(result)
    assert len(calls) == 2


def test_cooperative_deadline_discards_late_answer_but_preserves_known_spend(monkeypatch):
    item = request()
    clock = iter([0, 0, 2, 2])
    monkeypatch.setattr("agentloop.judgments.time.perf_counter", lambda: next(clock))
    answer = JudgmentAnswer(True, usage=JudgeUsage(cost_usd=0.04, cost_basis="reported"))
    result = session(answer).evaluate(item, enabled=True, timeout_s=1)
    assert result["evaluation"]["status"] == "timeout"
    assert result["evaluation"]["value"] is None
    assert result["invocation"]["usage"]["cost_usd"] == 0.04


def test_unknown_answers_and_partial_usage_are_retained():
    answer = JudgmentAnswer(
        status="unknown",
        reason="insufficient_evidence",
        usage=JudgeUsage(input_tokens=7, token_basis="reported"),
    )
    result = session(answer).evaluate(request(), enabled=True)
    assert result["evaluation"]["status"] == "unknown"
    assert result["evaluation"]["usage"]["input_tokens"] == 7
    assert result["evaluation"]["usage"]["output_tokens"] is None
    validate_judgment_record(result)


def test_cache_cost_and_provenance_are_not_double_counted_or_mutable():
    trace = pipeline()
    answer = JudgmentAnswer(True, usage=JudgeUsage(3, 1, 0.04, "reported", "reported"))
    evaluator = session(answer)
    first = evaluator.evaluate(request(trace), enabled=True)
    attach_judgment(trace, first)
    first["evaluation"]["value"] = False
    second = evaluator.evaluate(request(trace), enabled=True)
    assert second["evaluation"]["value"] is True
    assert second["invocation"]["cache_hit"]
    assert second["invocation"]["usage"]["cost_usd"] == 0
    assert second["evaluation"]["usage"]["cost_usd"] == 0.04
    attach_judgment(trace, second)
    attach_judgment(trace, second)
    view = read_judgments(trace)
    assert len(view["records"]) == 2 and view["known_cost_usd"] == 0.04
    assert view["cost_complete"]
    assert view["records"][0]["evaluation"]["id"] == view["records"][1]["evaluation"]["id"]


def test_cache_invalidates_evidence_question_config_revision_and_implementation():
    class Backend:
        identity = identity()

        def judge(self, item, **options):
            return JudgmentAnswer(True)

    backend = Backend()
    evaluator = JudgmentSession(backend)
    trace = pipeline()
    first = evaluator.evaluate(request(trace), enabled=True)
    assert evaluator.evaluate(request(trace), enabled=True)["invocation"]["cache_hit"]
    trace.events[0].output_text = "new payload, not forwarded"
    assert not evaluator.evaluate(request(trace), enabled=True)["invocation"]["cache_hit"]
    for changes in (
        {"version": "2"},
        {"revision": "2"},
        {"implementation": "different"},
        {"config_hash": fingerprint({"x": 3})},
    ):
        backend.identity = replace(backend.identity, **changes)
        result = evaluator.evaluate(request(trace), enabled=True)
        assert not result["invocation"]["cache_hit"]
        assert result["cache_key"] != first["cache_key"]
    item = request(trace, JudgmentSpec("Different question?", "boolean"))
    assert not evaluator.evaluate(item, enabled=True)["invocation"]["cache_hit"]
    assert not JudgmentSession(backend).evaluate(item, enabled=True)["invocation"]["cache_hit"]


def test_cache_is_bounded_and_can_be_disabled_or_cleared():
    evaluator = session(cache_size=1)
    one, two = request(), request(spec=JudgmentSpec("Other?", "boolean"))
    evaluator.evaluate(one, enabled=True)
    evaluator.evaluate(two, enabled=True)
    assert not evaluator.evaluate(one, enabled=True)["invocation"]["cache_hit"]
    evaluator.clear_cache()
    assert not evaluator.evaluate(one, enabled=True)["invocation"]["cache_hit"]
    evaluator = session(cache_size=0)
    evaluator.evaluate(one, enabled=True)
    assert not evaluator.evaluate(one, enabled=True)["invocation"]["cache_hit"]


def test_identity_change_during_callback_cannot_create_valid_cache_entry():
    class Backend:
        identity = identity()

        def judge(self, item, **options):
            self.identity = replace(self.identity, revision="changed")
            return JudgmentAnswer(True)

    result = JudgmentSession(Backend()).evaluate(request(), enabled=True)
    assert result["evaluation"]["status"] == "invalid_result"
    validate_judgment_record(result)


def test_async_result_is_closed_and_fatal_errors_propagate():
    async def async_judge(item, **options):
        return JudgmentAnswer(True)

    with pytest.raises(ValueError, match="synchronous"):
        LocalCallbackJudge(identity(), async_judge)
    evaluator = JudgmentSession(
        LocalCallbackJudge(identity(), lambda item, **options: async_judge(item))
    )
    assert evaluator.evaluate(request(), enabled=True)["evaluation"]["status"] == "invalid_result"

    def fatal(item, **options):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        JudgmentSession(LocalCallbackJudge(identity(), fatal)).evaluate(request(), enabled=True)


@pytest.mark.parametrize("mutation", ["body", "status", "duration", "removed", "run"])
def test_stale_source_never_survives_as_effective_answer(mutation):
    trace = pipeline()
    result = session().evaluate(request(trace), enabled=True)
    attach_judgment(trace, result)
    if mutation == "body":
        trace.events[0].output_text = "changed"
    elif mutation == "status":
        trace.events[0].status = "error"
    elif mutation == "duration":
        trace.events[0].duration_ms += 1
    elif mutation == "removed":
        trace.events.pop(0)
    else:
        trace.run_id = "different"
    view = read_judgments(trace)
    assert view["status"] == "incomplete"
    assert view["records"][0]["effective_status"] == "stale"
    assert view["records"][0]["effective_value"] is None
    with pytest.raises(ValueError):
        attach_judgment(trace, result)


def test_invalid_artifacts_are_not_silently_trusted_even_with_recomputed_hash():
    trace = pipeline()
    result = session().evaluate(request(trace), enabled=True)
    result["evaluation"]["value"] = "yes"
    result["record_hash"] = fingerprint(
        {key: value for key, value in result.items() if key != "record_hash"}
    )
    with pytest.raises(ValueError):
        attach_judgment(trace, result)
    trace.metadata[JUDGMENT_KEY] = {"schema_version": "1.0", "records": [result]}
    assert read_judgments(trace)["status"] == "invalid"
    trace.metadata[JUDGMENT_KEY]["schema_version"] = "9.0"
    assert read_judgments(trace)["status"] == "unsupported"


def test_duplicate_invocations_are_invalid_and_host_metadata_is_preserved():
    trace = pipeline()
    original = copy.deepcopy(trace.metadata)
    result = session().evaluate(request(trace), enabled=True)
    attach_judgment(trace, result)
    assert {key: value for key, value in trace.metadata.items() if key != JUDGMENT_KEY} == original
    trace.metadata[JUDGMENT_KEY]["records"].append(copy.deepcopy(result))
    assert read_judgments(trace)["status"] == "invalid"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: JudgmentSpec("Question", "choice", ["a", "a"]),
        lambda: JudgmentSpec("Question", "score", minimum=0, maximum=float("inf")),
        lambda: JudgmentSpec("Question", "probability", minimum=0, maximum=1),
        lambda: JudgmentSpec("Question", "boolean", choices=["yes"]),
        lambda: JudgmentUncertainty(confidence=1.01),
        lambda: JudgmentUncertainty(calibration_status="measured"),
        lambda: JudgeUsage(cost_usd=0.1),
        lambda: JudgeUsage(input_tokens=True, token_basis="reported"),
        lambda: JudgeUsage(cost_usd=1, cost_basis="not_dispatched"),
        lambda: JudgmentAnswer(value=False, status="unknown", reason="abstained"),
        lambda: JudgmentAnswer(value=float("nan")),
        lambda: judgment_request(pipeline(), JudgmentSpec("Question", "boolean"), ["missing"]),
    ],
)
def test_invalid_contract_configuration_fails_before_execution(factory):
    with pytest.raises(ValueError):
        factory()
