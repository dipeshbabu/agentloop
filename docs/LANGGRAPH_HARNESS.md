# LangGraph harness adapter

This opt-in adapter controls explicit dispatches in **LangGraph 1.2.11**. It uses
public node registration, `get_runtime().execution_info`, and `GraphBubbleUp`
control flow. Active runnable wrapping checks the installed version before use;
other versions need compatibility tests before widening the guard. LangGraph is
optional: importing this module or using disabled mode does not import its SDK.
The adapter requires no provider credentials and mutates no graph registries.

## Try the offline graph

From a checkout with the development environment installed:

```bash
uv run --isolated --frozen --with langgraph==1.2.11 python examples/langgraph_harness.py
uv run --isolated --frozen --with langgraph==1.2.11 python -m pytest tests/test_langgraph_harness.py tests/test_langgraph_conformance.py tests/test_enforcement_conformance.py -q
```

The [example](../examples/langgraph_harness.py) builds the same graph with a
model-call budget, then with an identical-call guard. For either guard, disabled
and shadow modes run two fake model calls and two fake tool calls and return
`{"value": 12}`. Enforced mode runs only the first pair; the second model callable
never starts. The budget denies it, and the loop guard stops admission. These
are synthetic correctness checks, not measured quality or savings claims.

## Configure explicit boundaries

```python
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import HarnessConfig
from agentloop.integrations.langgraph_harness import LangGraphHarness

adapter = LangGraphHarness(
    HarnessConfig(
        "shadow",
        (budget_policy(BudgetLimits(max_model_calls=3), boundaries={"model"}),),
    ),
    boundaries={"iteration", "model", "tool", "completion"},
)

# Wrap the actual callables used by your nodes:
# model_call = adapter.model(model_call, dispatch=..., usage_reader=...)
# tool_call = adapter.tool(tool_call)
# node = adapter.node("research")(node)
# builder.add_node("research", node)
# controlled = adapter.runnable(builder.compile())
# result = controlled.invoke(initial_state)
```

Wrap nodes **before** adding them to the builder. Calls decorated by an active
adapter require its controlled runnable as the entrypoint. `boundaries` is an
explicit declaration, not automatic discovery. Its default is `iteration` and
`completion`; policies requesting undeclared hooks fail during construction.
An unwrapped callable is outside enforcement even if its boundary is declared.
Use the original app through `controlled.app` for state inspection. Only the
four entrypoints below are exposed; `batch`, configuration methods, and other
execution APIs are not silently forwarded. Wrap a new configured runnable
explicitly before invoking it.

| Boundary | Supported coverage | Limits |
| --- | --- | --- |
| Node (`iteration`) | Decorated sync/async nodes, before and after dispatch | Generator nodes rejected; cached/skipped nodes do not dispatch or consume node admission |
| Model | `adapter.model()` sync, async, generator and async-generator callables | Hidden provider calls and SDK-internal retries not intercepted |
| Tool | `adapter.tool()` with the same four callable lifecycles | Unwrapped tools, side effects within an admitted callable, and automatic compensation not controlled |
| Compiled stream | Lazy `stream`/`astream`; each resume/close restores caller context | Not a policy hook per chunk; already emitted data cannot be recalled |
| Completion | Before/after returning a normal `invoke`/`ainvoke` result or exhausting a root stream | A delivery boundary, not a quality judgment or proof that an interrupted/checkpointed workflow has finished; absent on error, cancellation or early close |

Streaming model/tool usage collectors follow the existing
[budget contract](BUDGETS.md): cumulative usage replaces earlier observations;
unknown usage stays unknown. Limits remain cooperative admission controls and
best-effort spending bounds, not hard provider billing caps.

## Identity, retries, and tracing

Each root invocation creates its own synchronized `HarnessRun`. `adapter.last_run`
is local to the caller's execution context, including failed runs. Read it inside
the async task that invoked the graph. Streams start their run at first resume.
Repeated wrapping with the same adapter/settings returns the existing wrapper;
changing settings requires the original callable.

Node branches default to the public runtime task ID. `node_attempt > 1` marks a
framework retry of that node; child model/tool calls are not double-counted as
framework retries. A new graph step normally has a new task ID. To compare
sequential iterations for loop detection, pass a stable `branch_id` to `node()`
and, optionally, a synchronous `step_reader` returning `StepInfo` from the node's
original arguments. Declare retry safety honestly. The reader is trusted host
code and must be side-effect free; inputs are passed unchanged and not retained
by the adapter. Missing fingerprints remain qualified evidence. See
[loop guards](LOOP_GUARDS.md) before choosing branch/fingerprint semantics for
parallel graphs.

Guard control and evidence failures travel through a private subclass of the
SDK's public `GraphBubbleUp`, bypassing framework retries and error handlers.
The controlled root restores the original AgentLoop exception. Already admitted
parallel work is not rolled back. Ordinary exceptions retain the framework's
handling, including `NodeCancelledError` for a node that raises its own
`CancelledError`. External cancellation propagates and closes active work.
The adapter does not install a retry policy or error handler.

Use `trace_agent` around the controlled invocation/stream consumption, and existing
`trace_node` decorators around controlled nodes if node spans are wanted. The
adapter preserves the current trace and parent and creates no extra spans.
For a stream, keep its owning trace open until exhaustion/close. It binds captured
trace context separately for each resume, never across a yield to the caller.
Wrapping a trace-producing streaming adapter inside this control wrapper is not
a supported trace-ownership arrangement; put the trace outside instead.

## Rollback and compatibility evidence

Start with shadow mode. To disable controls, rebuild the same graph from the
original functions with `HarnessConfig("disabled")`: decorators and runnable
wrapping then return the originals. Changing a separate adapter cannot remove
wrappers already compiled into an existing graph. No database or trace migration
is needed, and existing tracing integration APIs are unchanged.

Shared [enforcement tests](../tests/test_enforcement_conformance.py) run against
custom Python hooks and a protocol graph. The separate
[pinned SDK suite](../tests/test_langgraph_harness.py) checks real state delivery,
runtime injection, parent linkage, retries, denial, loop/budget controls,
cancellation, and root streaming. The existing tracing conformance suite also
runs unchanged. CI runs the pinned suite and offline example on Python 3.13;
protocol tests run on all supported CI Python versions.

Public SDK references:
[node registration](https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_node)
and [fault tolerance, retries and runtime execution info](https://docs.langchain.com/oss/python/langgraph/fault-tolerance).
