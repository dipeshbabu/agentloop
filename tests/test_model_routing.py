from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentloop import trace_agent
from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_types import ContextTokenCount
from agentloop.harness import Harness, HarnessConfig, HarnessDeniedError
from agentloop.model_routing import ModelRouter, routing_policy
from agentloop.routing_types import (
    ROUTING_KEY,
    ModelBackend,
    ModelCapabilities,
    ModelIdentity,
    RouteProviderError,
    RouteResponseInfo,
    RoutingConfig,
    RoutingError,
    RoutingRequest,
)

ORIGINAL = ModelIdentity("fixture", "original")
TARGET = ModelIdentity("fixture", "target")


def usage(tokens, *, complete=True):
    return ResourceUsage(tokens=tokens, token_provenance="provider", complete=complete)


def response(model, tokens=3):
    return SimpleNamespace(model=model, usage=usage(tokens), content="PRIVATE response text")


def inspect_response(value):
    return RouteResponseInfo(value.usage, value.model)


def backend(identity, invoke=None, **changes):
    caps = ModelCapabilities(
        "fixture-caps-v1",
        128,
        parameters=(
            "messages",
            "model",
            "max_tokens",
            "temperature",
            "response_format",
            "tools",
            "stream",
        ),
        output_modes=("text", "json_object", "json_schema"),
        tool_calling=True,
    )
    values = dict(
        identity=identity,
        capabilities=caps,
        invoke=invoke or (lambda payload: response(payload["model"])),
        token_counter=lambda payload: ContextTokenCount(
            10, "user_supplied", "fixture-token-count-v1"
        ),
        inspect_response=inspect_response,
        dispatch=DispatchOptions(Reservation(tokens=20, provenance="upper_bound")),
    )
    values.update(changes)
    return ModelBackend(**values)


def config(**changes):
    return replace(
        RoutingConfig(
            "route", "1", "answer", "independent-quality-v1", ORIGINAL, TARGET, (ORIGINAL, TARGET)
        ),
        **changes,
    )


def router(mode="enforce", *, settings=None, original=None, target=None, budgets=(), **kwargs):
    settings = settings or config()
    run = Harness(
        HarnessConfig(mode=mode, policies=(routing_policy(settings), *budgets))
    ).start_run()
    return ModelRouter(
        run,
        config=settings,
        backends=(original or backend(ORIGINAL), target or backend(TARGET)),
        **kwargs,
    ), run


def request(**changes):
    return RoutingRequest(
        {
            "model": "original",
            "messages": [
                {"role": "system", "content": "PRIVATE system instructions"},
                {"role": "user", "content": "PRIVATE task constraints"},
            ],
            "max_tokens": 12,
            "temperature": 0.0,
            **changes,
        }
    )


def record(value):
    return next(iter(value.export_evidence()["records"].values()))


def test_manual_route_preserves_owned_parameters_and_sdk_result_identity():
    captured = []
    expected = response("reported-target-revision")

    def invoke(payload):
        captured.append(payload)
        return expected

    value, _ = router(target=backend(TARGET, invoke))
    original = request(response_format={"type": "json_object"})
    assert value(original) is expected
    assert captured == [{**original.payload, "model": "target"}]
    captured[0]["messages"].clear()
    assert original.payload["messages"]
    evidence = record(value)
    assert evidence["requested"] == ORIGINAL.to_dict()
    assert evidence["attempts"][0]["selected"] == TARGET.to_dict()
    assert evidence["attempts"][0]["provider_reported_model"] == "reported-target-revision"
    assert evidence["attempts"][0]["usage"]["tokens"] == 3
    assert evidence["capability_checks"][0]["supported"]
    assert evidence["status"] == "completed" and "PRIVATE" not in str(evidence)


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_disabled_and_shadow_keep_original_without_target_dispatch(mode):
    calls = []
    value, _ = router(
        mode,
        original=backend(ORIGINAL, lambda payload: calls.append(payload) or response("original")),
        target=backend(TARGET, lambda payload: pytest.fail("target dispatched")),
    )
    original = request()
    value(original)
    assert calls == [original.payload]
    assert (
        not value.export_evidence()["records"]
        if mode == "disabled"
        else record(value)["reason"] == "shadow_original"
    )


@pytest.mark.parametrize(
    "gap",
    [
        "parameters",
        "tools",
        "schema",
        "context",
        "count",
        "modality",
        "adapter",
        "output_bound",
        "malformed_role",
    ],
)
def test_missing_capability_fails_before_dispatch_or_restores_original(gap):
    target = backend(TARGET, lambda payload: pytest.fail("unsupported route dispatched"))
    payload = request().payload
    if gap == "parameters":
        payload["unknown_option"] = "PRIVATE value"
    elif gap == "tools":
        target = replace(target, capabilities=replace(target.capabilities, tool_calling=False))
        payload["tools"] = [{"type": "function", "function": {"name": "lookup"}}]
    elif gap == "schema":
        target = replace(target, capabilities=replace(target.capabilities, output_modes=("text",)))
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"schema": {"type": "object"}},
        }
    elif gap == "context":
        target = replace(target, capabilities=replace(target.capabilities, context_window=20))
    elif gap == "count":
        target = replace(
            target,
            token_counter=lambda payload: ContextTokenCount(
                1, "estimated_words", "word-fixture-v1"
            ),
        )
    elif gap == "modality":
        payload["messages"][-1]["content"] = [
            {"type": "image_url", "image_url": {"url": "private:image"}}
        ]
    elif gap == "adapter":
        target = replace(target, capabilities=replace(target.capabilities, adapter="unsupported"))
    elif gap == "output_bound":
        del payload["max_tokens"]
    else:
        payload["messages"][0]["role"] = {"bad": "role"}
    value, _ = router(target=target)
    with pytest.raises(RoutingError, match="unsupported_route"):
        value(RoutingRequest(payload))
    assert record(value)["attempts"] == []
    captured = []
    value, _ = router(
        settings=config(on_unsupported="original"),
        target=target,
        original=backend(ORIGINAL, lambda data: captured.append(data) or response("original")),
    )
    value(RoutingRequest(payload))
    assert captured == [payload] and record(value)["reason"] == "unsupported_original"


def test_fallback_charges_failed_and_successful_calls_to_same_budget():
    failure = RouteProviderError("rate_limit", info=RouteResponseInfo(usage(2), "target"))

    def fail(payload):
        raise failure

    value, run = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("rate_limit",)),
        target=backend(TARGET, fail),
        budgets=(budget_policy(BudgetLimits(max_model_calls=2, max_tokens=40)),),
    )
    assert value(request()).model == "original"
    attempts = record(value)["attempts"]
    assert [item["status"] for item in attempts] == ["failed", "completed"]
    assert [item["usage"]["tokens"] for item in attempts] == [2, 3]
    assert all(item["harness_call_id"] for item in attempts)
    with pytest.raises(HarnessDeniedError):
        run.wrap(lambda: pytest.fail("call cap bypassed"), boundary="model", branch_id="other")()


def test_budget_exhaustion_blocks_fallback_before_provider_invocation():
    def fail(payload):
        raise RouteProviderError("unavailable")

    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("unavailable",)),
        target=backend(TARGET, fail),
        original=backend(ORIGINAL, lambda data: pytest.fail("fallback escaped budget")),
        budgets=(budget_policy(BudgetLimits(max_model_calls=1)),),
    )
    with pytest.raises(HarnessDeniedError):
        value(request())
    assert [item["provider_invoked"] for item in record(value)["attempts"]] == [True, False]


@pytest.mark.parametrize(
    "failure", [ValueError("PRIVATE failure"), KeyboardInterrupt(), RouteProviderError("timeout")]
)
def test_unconfigured_failure_or_cancellation_never_falls_back(failure):
    def fail(payload):
        raise failure

    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("rate_limit",)),
        target=backend(TARGET, fail),
        original=backend(ORIGINAL, lambda data: pytest.fail("unconfigured fallback")),
    )
    with pytest.raises(type(failure)) as caught:
        value(request())
    assert caught.value is failure and len(record(value)["attempts"]) == 1
    assert "PRIVATE failure" not in str(record(value))


class SDKStream:
    def __init__(self, values):
        self.values = iter(values)
        self.closed = 0

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self.values)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed += 1


def streaming_backend(identity, stream):
    base = backend(identity)
    return replace(
        base, capabilities=replace(base.capabilities, streaming=True), stream=lambda payload: stream
    )


def test_streaming_cancellation_closes_sdk_stream_and_never_falls_back():
    chunk = response("target", 2)
    chunk.usage = usage(2, complete=False)
    sdk = SDKStream([chunk, response("target", 4)])
    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("timeout",)),
        target=streaming_backend(TARGET, sdk),
        original=backend(ORIGINAL, lambda data: pytest.fail("fallback after cancellation")),
    )
    stream = value.stream(request(stream=True))
    assert next(stream) is chunk
    stream.close()
    evidence = record(value)
    assert sdk.closed == 1 and evidence["status"] == "cancelled"
    assert evidence["attempts"][0]["status"] == "cancelled"
    assert evidence["attempts"][0]["usage"]["tokens"] == 2
    assert not evidence["attempts"][0]["usage"]["complete"]


def test_stream_failure_after_output_cannot_replay_on_another_model():
    failure = RouteProviderError("rate_limit", info=RouteResponseInfo(usage(4)))
    sdk = SDKStream([response("target", 2), failure])
    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("rate_limit",)),
        target=streaming_backend(TARGET, sdk),
        original=backend(ORIGINAL, lambda data: pytest.fail("partial output repeated")),
    )
    stream = value.stream(request(stream=True))
    assert next(stream).model == "target"
    with pytest.raises(RouteProviderError) as caught:
        next(stream)
    assert caught.value is failure and sdk.closed == 1
    assert (
        len(record(value)["attempts"]) == 1 and record(value)["attempts"][0]["usage"]["tokens"] == 4
    )


def test_stream_usage_is_latest_snapshot_not_sum_of_cumulative_chunks():
    sdk = SDKStream([response("target", 2), response("target", 5)])
    value, _ = router(
        target=streaming_backend(TARGET, sdk), budgets=(budget_policy(BudgetLimits(max_tokens=20)),)
    )
    assert len(list(value.stream(request(stream=True)))) == 2
    assert record(value)["attempts"][0]["usage"]["tokens"] == 5 and sdk.closed == 1


def test_stream_fallback_before_first_chunk_is_bounded_and_metered():
    failed = SDKStream([RouteProviderError("rate_limit", info=RouteResponseInfo(usage(1)))])
    fallback = SDKStream([response("original", 3)])
    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("rate_limit",)),
        target=streaming_backend(TARGET, failed),
        original=streaming_backend(ORIGINAL, fallback),
    )
    assert [item.model for item in value.stream(request(stream=True))] == ["original"]
    assert failed.closed == fallback.closed == 1
    assert [item["usage"]["tokens"] for item in record(value)["attempts"]] == [1, 3]


def test_policy_requires_adapter_and_default_cross_provider_route_is_rejected():
    settings = config()
    run = Harness(HarnessConfig(mode="enforce", policies=(routing_policy(settings),))).start_run()
    with pytest.raises(HarnessDeniedError):
        run.wrap(lambda: pytest.fail("unbound route"), boundary="model", branch_id="answer")()
    remote = ModelIdentity("other", "target")
    with pytest.raises(ValueError, match="cross-provider"):
        config(target=remote, allowlist=(ORIGINAL, remote))
    with pytest.raises(ValueError, match="allowlisted"):
        config(allowlist=(ORIGINAL,))


def test_evidence_is_copied_bounded_and_native_without_raw_payload():
    with trace_agent("routing") as trace:
        value, _ = router(max_records=1)
        value(request())
    assert trace.metadata[ROUTING_KEY] == value.export_evidence()
    copied = value.export_evidence()
    copied["records"].clear()
    with pytest.raises(RoutingError, match="evidence_limit"):
        value(request())
    assert "PRIVATE" not in str([trace.metadata[ROUTING_KEY], value.run.export_evidence()])


def test_sync_only_harness_does_not_require_an_artificial_generator_lifecycle():
    from agentloop.harness import PYTHON_CAPABILITIES

    settings = config()
    capabilities = replace(PYTHON_CAPABILITIES, execution_kinds=frozenset({"sync"}))
    run = Harness(
        HarnessConfig(mode="enforce", policies=(routing_policy(settings),)), capabilities
    ).start_run()
    value = ModelRouter(run, config=settings, backends=(backend(ORIGINAL), backend(TARGET)))
    assert value(request()).model == "target"
    with pytest.raises(RoutingError, match="unsupported_route"):
        list(value.stream(request(stream=True)))


def test_usage_reported_by_failed_stream_close_is_retained_without_hiding_cancellation():
    class ChargedClose(SDKStream):
        def close(self):
            self.closed += 1
            raise RouteProviderError("provider_error", info=RouteResponseInfo(usage(5), "target"))

    sdk = ChargedClose([response("target", 2), response("target", 4)])
    value, run = router(
        target=streaming_backend(TARGET, sdk), budgets=(budget_policy(BudgetLimits(max_tokens=20)),)
    )
    stream = value.stream(request(stream=True))
    next(stream)
    stream.close()
    attempt = record(value)["attempts"][0]
    assert attempt["status"] == "cancelled" and attempt["usage"]["tokens"] == 5
    assert attempt["cleanup_error"] == "stream_close_failed" and sdk.closed == 1
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("close usage ignored"),
            boundary="model",
            branch_id="other",
            dispatch=DispatchOptions(Reservation(tokens=16, provenance="upper_bound")),
        )()


def test_invalid_inspection_cannot_keep_a_stale_complete_usage_snapshot():
    reports = iter([RouteResponseInfo(usage(2), "target"), "invalid"])
    sdk = SDKStream([response("target", 2), response("target", 5)])
    target = replace(streaming_backend(TARGET, sdk), inspect_response=lambda chunk: next(reports))
    value, _ = router(target=target)
    list(value.stream(request(stream=True)))
    attempt = record(value)["attempts"][0]
    assert attempt["usage"]["tokens"] is None and not attempt["usage"]["complete"]
    assert attempt["last_known_usage"]["tokens"] == 2


def test_failed_fallback_is_not_retried_and_retains_both_failures():
    first = RouteProviderError("rate_limit", info=RouteResponseInfo(usage(1)))
    last = RouteProviderError("unavailable", info=RouteResponseInfo(usage(2)))

    def fail(error):
        def invoke(payload):
            raise error

        return invoke

    value, _ = router(
        settings=config(fallbacks=(ORIGINAL,), fallback_on=("rate_limit", "unavailable")),
        target=backend(TARGET, fail(first)),
        original=backend(ORIGINAL, fail(last)),
    )
    with pytest.raises(RouteProviderError) as caught:
        value(request())
    assert caught.value is last
    assert [item["usage"]["tokens"] for item in record(value)["attempts"]] == [1, 2]


def test_metadata_callbacks_cannot_dispatch_protected_model_work():
    value, run = router()
    hidden = run.wrap(
        lambda: pytest.fail("inspection dispatched a model"), boundary="model", branch_id="other"
    )
    bad_counter = replace(backend(TARGET), token_counter=lambda payload: hidden())
    value = ModelRouter(run, config=value.config, backends=(backend(ORIGINAL), bad_counter))
    with pytest.raises(RoutingError, match="unsupported_route"):
        value(request())
    assert record(value)["attempts"] == []


def test_bound_configuration_and_backend_registry_cannot_be_replaced():
    value, _ = router()
    with pytest.raises(TypeError):
        value.backends[TARGET] = backend(ORIGINAL)
    with pytest.raises(AttributeError):
        value.config = config(branch_id="unprotected")
    assert value(request()).model == "target"


def test_static_capability_failure_does_not_run_unnecessary_tokenization():
    target = replace(
        backend(TARGET), token_counter=lambda payload: pytest.fail("unnecessary tokenizer call")
    )
    value, _ = router(target=target)
    with pytest.raises(RoutingError):
        value(request(unknown_option="PRIVATE"))
    assert not record(value)["capability_checks"][0]["token_count_attempted"]
