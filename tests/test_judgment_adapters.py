from __future__ import annotations

from dataclasses import asdict

import pytest
from workflow_fixtures import pipeline

from agentloop import (
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSession,
    JudgmentSpec,
    JudgmentUncertainty,
    judgment_request,
)
from agentloop.judgment_adapters import LocalPredictorJudge, TypedServiceJudge, parse_typed_answer


def identity():
    return JudgeIdentity.configured(
        "test.adapter", "1", {"mode": "fixture"}, model_or_rule="fake-model"
    )


def request():
    return judgment_request(pipeline(), JudgmentSpec("Probability?", "probability"), ["classify"])


def test_local_predictor_uses_native_contract_without_inventing_usage():
    backend = LocalPredictorJudge(identity(), lambda item, **options: 0.8)
    result = JudgmentSession(backend).evaluate(request(), enabled=True)
    assert result["evaluation"]["value"] == 0.8
    assert result["evaluation"]["usage"]["cost_usd"] is None
    assert result["evaluation"]["uncertainty"]["confidence"] is None
    invalid = LocalPredictorJudge(identity(), lambda item, **options: True)
    assert (
        JudgmentSession(invalid).evaluate(request(), enabled=True)["evaluation"]["status"]
        == "invalid_result"
    )


def test_predictor_can_preserve_per_call_usage_and_uncertainty():
    answer = JudgmentAnswer(
        0.8,
        usage=JudgeUsage(cost_usd=0.01, cost_basis="calculated"),
        uncertainty=JudgmentUncertainty(
            confidence=0.7, calibration_status="measured", calibration_ref="heldout:calibration-v2"
        ),
    )
    backend = LocalPredictorJudge(identity(), lambda *args, **kwargs: answer)
    result = JudgmentSession(backend).evaluate(request(), enabled=True)["evaluation"]
    assert result["usage"]["cost_usd"] == 0.01
    assert result["uncertainty"]["calibration_ref"] == "heldout:calibration-v2"


def test_missing_credentials_and_configuration_are_explicit_without_transport_call():
    calls = []
    backend = TypedServiceJudge(identity(), lambda *args, **kwargs: calls.append(args))
    assert backend.availability == "missing_credentials"
    result = JudgmentSession(backend).evaluate(request(), enabled=True)
    assert result["evaluation"]["status"] == "unknown" and result["evaluation"]["value"] is None
    assert result["evaluation"]["usage"]["cost_usd"] == 0 and calls == []
    unconfigured = TypedServiceJudge(identity())
    assert unconfigured.availability == "not_configured"
    assert unconfigured.judge(request()).status == "unknown"


def test_typed_service_receives_owned_label_blind_data_and_preserves_usage():
    original = request()
    calls = []

    def send(payload, *, timeout_s):
        calls.append((payload.copy(), timeout_s))
        payload["spec"]["question"] = "mutated by remote-style test transport"
        return {"value": 0.75, "usage": asdict(JudgeUsage(11, 2, 0.002, "reported", "reported"))}

    backend = TypedServiceJudge(identity(), send, credentials_available=True)
    result = JudgmentSession(backend).evaluate(original, enabled=True, timeout_s=2)
    assert result["evaluation"]["value"] == 0.75
    assert result["evaluation"]["usage"]["cost_usd"] == 0.002
    assert original.spec.question == "Probability?"
    assert set(calls[0][0]) == {"run_id", "spec", "evidence"}
    assert calls[0][1] == 2


@pytest.mark.parametrize(
    "response",
    [
        "probably yes",
        {"value": 0.9, "explanation": "free-form data"},
        {"value": {"secret": "nested"}},
        {"value": float("nan")},
        {"value": 0.9, "usage": {"cost_usd": 3}},
        {"value": "x" * 65537},
    ],
)
def test_unstructured_or_invalid_response_becomes_private_error(response):
    backend = TypedServiceJudge(
        identity(), lambda *args, **kwargs: response, credentials_required=False
    )
    result = JudgmentSession(backend).evaluate(request(), enabled=True)
    assert result["evaluation"]["status"] == "error"
    assert result["evaluation"]["value"] is None
    assert result["evaluation"]["usage"]["cost_usd"] is None


def test_transport_timeouts_and_freeform_failures_remain_unknown():
    def timeout(*args, **kwargs):
        raise TimeoutError("private provider information")

    backend = TypedServiceJudge(identity(), timeout, credentials_required=False)
    result = JudgmentSession(backend).evaluate(request(), enabled=True)
    assert result["evaluation"]["status"] == "timeout"
    assert "private provider information" not in str(result)


def test_typed_abstention_is_preserved():
    response = asdict(JudgmentAnswer(status="unknown", reason="insufficient_evidence"))
    assert parse_typed_answer(response).status == "unknown"


def test_async_prediction_and_transport_cannot_leak_deferred_work():
    async def deferred(*args, **kwargs):
        return 0.9

    with pytest.raises(ValueError):
        LocalPredictorJudge(identity(), deferred)
    with pytest.raises(ValueError):
        TypedServiceJudge(identity(), deferred)
    for backend in (
        LocalPredictorJudge(identity(), lambda *args, **kwargs: deferred()),
        TypedServiceJudge(
            identity(), lambda *args, **kwargs: deferred(), credentials_required=False
        ),
    ):
        assert (
            JudgmentSession(backend).evaluate(request(), enabled=True)["evaluation"]["status"]
            == "error"
        )
