# Integration conformance harness

The shared [protocol harness](../tests/integration_conformance.py) and
[parameterized contracts](../tests/test_integration_conformance.py) exercise
adapter behavior with local fakes. They require no provider account, framework
service, or optional framework SDK.

The current factories cover OpenAI client resource methods, LangGraph builder
node registration, and CrewAI task execution. Each declares its tested surface,
capabilities, event type, and behavior without an active trace.

| Surface | Sync/async calls | Stream lifecycle | Model usage | Without active trace |
| --- | --- | --- | --- | --- |
| OpenAI client methods | Supported | Sync and async | Supported; absent usage is unavailable | Original call passes through |
| LangGraph builder nodes | Supported | Unsupported for node wrappers | Unsupported for node spans | Requires an active trace |
| CrewAI task methods | Supported | Unsupported for task wrappers | Unsupported for task spans | Requires an active trace |

The LangGraph declaration concerns `instrument_state_graph` node wrappers;
compiled `TracedRunnable` streaming is a separate surface with existing dedicated
tests. Crew-level trace creation is also tested separately from task wrappers.
Unsupported capabilities are named skips with reasons, not implicit omissions.
Run with `-ra` to display those declarations in the test result:

```bash
uv run python -m pytest tests/test_integration_conformance.py -q -ra
```

Shared checks assert exact return-object identity and original exception or
cancellation identity. They also verify argument forwarding, repeated
instrumentation without duplicate events, parent linkage/context restoration,
missing model usage, and exclusion of raw arguments, credentials, and returned
content from default telemetry. Stream cases cover deferred recording, full
consumption, close/aclose before and after consumption, iteration failure,
close failure, cancellation, and final usage.

## Add an adapter

1. Add a factory to `tests/integration_conformance.py` taking `(callable,
   instrumentation_repetitions)` and returning the instrumented callable.
   Construct the smallest protocol-compatible object needed by your adapter.
   Reapply the real instrumentation function for every repetition.
2. Add an `AdapterContract` to `ADAPTERS`. Name the precise surface being tested.
   Every key in `CAPABILITIES` must be `True` or a nonempty explanation of why
   that surface does not support it. Declare `without_trace` as `passthrough` or
   `requires_active_trace` and set the expected event type.
3. Run the shared suite. Add an adapter-specific fixture or contract when a
   protocol exposes a distinct behavior; do not weaken shared assertions to
   hide a regression. Keep the existing integration-specific tests for native
   object shapes and APIs that are outside this common call/stream protocol.
4. Document the adapter's supported surface and run its offline example before
   the full repository checks.

The harness is test-only. It is not a runtime plugin API or automatic package
discovery mechanism, and passing these fakes does not establish compatibility
with every future upstream SDK release.
