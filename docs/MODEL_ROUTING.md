# Explicit model-routing controls

This **Unreleased** API requires a source checkout. A route names one original
model, one target and an explicit allowlist. Token counts check context fit;
they never choose or authorize a smaller model. Task quality is evaluated
separately, as demonstrated by the [retained routing study](ROUTING_STUDY.md).

```python
from agentloop.harness import Harness, HarnessConfig
from agentloop.model_routing import ModelRouter, routing_policy
from agentloop.routing_types import ModelIdentity, RoutingConfig, RoutingRequest

original = ModelIdentity("my-provider", "original-model")
candidate = ModelIdentity("my-provider", "candidate-model")
settings = RoutingConfig(
    "reviewed-answer-route", "1", "answer", "my-task-scorer-v3",
    original, candidate, (original, candidate),
    fallbacks=(original,), fallback_on=("rate_limit", "unavailable"),
)
run = Harness(HarnessConfig(
    mode="shadow", policies=(routing_policy(settings),),
)).start_run("task-01")
router = ModelRouter(run, config=settings, backends=[original_backend, candidate_backend])
result = router(RoutingRequest({
    "model": "original-model",
    "messages": [{"role": "user", "content": "The application's task"}],
    "max_tokens": 256,
}))
```

This integration sketch requires two host-owned `ModelBackend` declarations.
Each supplies a typed identity, `ModelCapabilities`, a synchronous SDK invocation,
an exact model-specific token counter, a response inspector and optional
`DispatchOptions`. No provider SDK, model download or remote registry is loaded
by the router. Bind a new router/run to change its configuration or mode.

## Capabilities and modes

`ModelCapabilities` declares a versioned reference, adapter format
`json_chat_v1`, context window, accepted parameter names, output modes, input
modalities, tool calling and streaming support. Output modes are `text`,
`json_object` and `json_schema`; supported modality declarations are text, image
and audio. These are host integration contracts, not automatic model discovery
or proof that every provider implements every schema dialect.

Before substitution, the adapter checks all supplied parameter names, message
content kinds, tool use/history, output/schema mode and streaming requirements.
A request must declare one positive `max_tokens` or `max_completion_tokens`.
The backend counter returns `ContextTokenCount` with exact `provider`, `tokenizer`
or `user_supplied` provenance and a versioned reference. Input plus requested
output must fit the declared window. Word estimates cannot satisfy this check.
Static capability failures avoid unnecessary tokenization.

The counter must count the actual model's chat template and relevant request
fields. Counters and response inspectors are trusted, read-only callbacks;
protected model dispatch from them is rejected. They must not hide inference
or paid calls outside the run's accounting.

- **Disabled:** use the original SDK binding without routing evidence or policies.
- **Shadow:** record the target checks and invoke only the original binding.
- **Enforce:** use the explicitly configured target when its checks pass.

`on_unsupported="error"` rejects an unsupported route before provider invocation.
`on_unsupported="original"` preserves the original request and delegates its
validation to the original SDK. It does not establish that the original model
supports the rejected target's capabilities. Missing harness lifecycle support
or a missing original stream factory is an explicit error. Parameters are never
silently removed or rewritten; only the selected model value changes on a route.
Caller data is copied, including nested provider options.

Every endpoint must be allowlisted. Cross-provider substitution additionally
requires `allow_cross_provider=True`; it is disabled by default. Backend identity,
capability and tokenizer declarations must describe the actual SDK binding.
The library is not a sandbox around arbitrary host callback code.

## Failure, fallback and streaming

Fallback is disabled unless both endpoints and failure categories are configured.
At most two distinct fallbacks are allowed. A host SDK adapter classifies a
provider failure as `RouteProviderError` with a bounded category (`rate_limit`,
`unavailable`, `timeout` or `provider_error`) and optional `RouteResponseInfo`.
Unknown exceptions propagate. Fallback capabilities are checked before each
attempt; unsupported fallback fails explicitly.

Every attempt uses the same `HarnessRun` and its budgets. Supply conservative
`DispatchOptions(Reservation(...))` separately for every backend when enforcing
token or cost bounds. A fallback is marked as a harness retry. Admission denial
prevents SDK invocation. A stopped run or cancellation cannot trigger fallback.
SDK automatic retries must be disabled or fully represented by the host adapter;
an invocation count is not a guarantee about unobserved transport retries.

`inspect_response` returns `RouteResponseInfo(usage, reported_model)`. Usage is
an exclusive `ResourceUsage` snapshot, not an increment. Streaming readers must
identify complete final usage explicitly. Missing usage remains unknown, and
collector errors cannot reuse an earlier complete snapshot as if it were current.
Provider usage identifiers are namespaced and hashed in routing evidence.

For streaming, supply a synchronous stream factory that returns the SDK iterator,
declare streaming support, and call `router.stream(RoutingRequest(payload))` with
`stream=True`. Original chunk objects are yielded, the underlying stream is closed,
and cancellation preserves partial usage. Fallback can occur only before the first
chunk reaches the caller; output is never silently replayed from another model.
Async SDK callbacks are unsupported in this first adapter.

The shared harness now accepts optional `error_usage_reader(exception)`. It reads
available SDK failure or close usage before the after-hook, without putting an
exception or payload in `HookContext`. It does not run for denied or successful
work. Reader failures cannot replace the original exception/cancellation. The
default remains unchanged. Router attempts use this path to retain failure usage.

Do not wrap this adapter and `ContextModel` around the same physical model call:
each owns admission and would otherwise double-count work. Nesting the router
inside an active context adapter is explicitly rejected. A shared dispatch
integration is required before composing those transformations.

## Evidence and quality

`router.export_evidence()` and active trace metadata at `agentloop.model_routing`
record route/version/configuration, requested and selected identities, the
provider-reported model when available, capability results, request fingerprints,
attempt outcomes, usage, fallback reason and latency. Provider-reported aliases
are observations, not independent attestation of a model artifact.

Receipts omit prompt bodies, response text, chunks and exception messages.
Exports are copies; configured evidence bounds stop further admission explicitly.
Streaming latency covers the dispatch lifecycle, including consumer delays.
The SDK callback's entry marker is not proof of a network request when the SDK
uses caching or other internal behavior.

The quality reference records the caller's independent scorer. The router does
not call a judge, treat a capability pass as correctness, repair a wrong answer,
or promote a route from historical scores. Returning to the original configuration
or disabling routing is the rollback; completed requests cannot be undone.
