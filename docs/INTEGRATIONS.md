# AgentLoop Framework Integrations

AgentLoop integrations are designed for low-friction instrumentation. A team
should be able to wrap an existing agent and inspect latency, token, retry, and
tool-call waste without changing its framework.

This guide covers the dependency-free integration helpers currently shipped in `agentloop.integrations` and the generic decorator SDK for custom agents.

## Generic Python decorator SDK

Use this path when the team has a custom agent loop rather than LangGraph, CrewAI, or another framework.

```python
import agentloop

agentloop.init(
    project_id="agentloop-demo",
    export_dir="runs",
    auto_upload=False,
)

@agentloop.trace_model(name="planner_llm", model="gpt-4.1-mini")
def plan(question: str) -> str:
    return client.responses.create(model="gpt-4.1-mini", input=question).output_text

@agentloop.trace_tool(name="web_search")
def web_search(query: str) -> list[str]:
    return search(query)

@agentloop.traceable(root=True, agent_name="research_agent")
def run_agent(question: str) -> str:
    plan_text = plan(question)
    docs = web_search(question)
    return synthesize(plan_text, docs)

run_agent("Compare agent observability tools")
```

Available decorators:

- `@agentloop.traceable(root=True, agent_name="...")` starts a full trace for a custom agent entrypoint.
- `@agentloop.trace_tool(name="...")` records a tool/function span.
- `@agentloop.trace_model(name="...", model="...")` records a model-call-like span.

For a custom Python agent, add the three decorators, run the agent once, and
inspect or export the resulting trace.

## OpenAI SDK

```python
from openai import OpenAI
from agentloop import trace_agent
from agentloop.integrations.openai import instrument_openai_client

client = instrument_openai_client(OpenAI())

with trace_agent("research_agent") as trace:
    response = client.responses.create(
        model="gpt-4.1-mini",
        input="Research three competitors and summarize the differences.",
    )

trace.export_json("runs/openai_agent.json")
```

Supported call sites when present:

- `client.responses.create(...)`
- `client.chat.completions.create(...)`

AgentLoop records:

- latency
- model name
- input tokens
- output tokens
- status/error
- message count
- tool count

You can also wrap any OpenAI-like callable directly:

```python
from agentloop.integrations.openai import instrument_callable

wrapped_create = instrument_callable(client.responses.create, name="openai.responses.create")
```

Instrumentation is idempotent: instrumenting the same client or callable more than
once is a no-op, so one request records one event. Streaming responses
(`stream=True`) are finalized when the stream is fully consumed, closed, fails, or
is cancelled — not when the iterator is created — so the recorded latency covers
consumption and final usage is captured when the SDK reports it in the stream.
Calls made without an active trace are not recorded and do not raise.

## LangGraph

```python
from agentloop.integrations.langgraph import instrument_state_graph, trace_runnable

builder = StateGraph(State)
instrument_state_graph(builder)

builder.add_node("retrieve", retrieve)
builder.add_node("synthesize", synthesize)

app = trace_runnable(builder.compile(), name="research_graph")
app.invoke({"question": "What is agent-loop optimization?"})
app.export_last_trace("runs/langgraph_agent.json")
```

AgentLoop wraps nodes added after `instrument_state_graph(...)` and records each node as a tool-call span.

## CrewAI

```python
from agentloop.integrations.crewai import instrument_agent, instrument_crew, instrument_task

researcher = instrument_agent(researcher)
research_task = instrument_task(research_task)
crew = instrument_crew(crew, name="market_research_crew")

crew.kickoff()
```

Supported Crew-like methods when present:

- Crew: `kickoff`, `kickoff_async`
- Task: `execute`, `execute_sync`, `execute_async`, `run`
- Agent: `execute_task`, `run`, `invoke`

The integration is dependency-free and works by patching object methods in-place.

## Vercel AI SDK / JavaScript agents

For JS/TS applications, export telemetry events as JSON and convert them into an AgentLoop trace on the backend:

```python
from agentloop.integrations.vercel_ai import trace_from_vercel_ai_events

trace = trace_from_vercel_ai_events(events, name="vercel_ai_agent")
trace.export_json("runs/vercel_ai_agent.json")
```

Expected event fields are flexible:

```json
{
  "type": "generate",
  "name": "generateText",
  "duration_ms": 1200,
  "model": "gpt-4.1-mini",
  "input_tokens": 900,
  "output_tokens": 240,
  "metadata": {"step": "draft"}
}
```

Event type mapping:

- `text`, `generate`, `stream`, `model` -> `model_call`
- `tool`, `tool-call`, `step` -> `tool_call`

## OpenTelemetry trace and span IDs

OTLP requires trace IDs to be 32 lowercase hexadecimal characters and span IDs
16, and forbids all-zero values. AgentLoop lets you choose custom run and event
IDs (the local/API constructors and the OpenAI Agents bridge all allow them),
so when exporting to OTLP (`export-otel`, `trace_to_otel`, or the OpenAI Agents
processor) AgentLoop maps every identifier to a valid one:

- An **already-valid** ID (correct width, non-zero, lowercase hex) is preserved
  exactly — so a trace imported from OTLP round-trips back out unchanged.
- **Every other** ID — non-hex, wrong width, empty, or all-zero — is mapped
  deterministically with SHA-256. Distinct native IDs stay distinct (for example
  `a` and `0a` do **not** collapse to the same value), preserving within-trace
  uniqueness and parent linkage.

The original native identifiers are preserved on the exported span attributes
(`agentloop.native_event_id`, `agentloop.native_parent_id`, and, from the OpenAI
Agents bridge, `agentloop.native_span_id` / `agentloop.native_trace_id`; the run
ID is also on `agentloop.run_id`), so a remapped ID is still diagnosable.

> **Compatibility note.** Importing an OTLP trace now preserves the full 32-hex
> trace ID rather than truncating it to the last 16, so a trace imported from
> OTLP has a run ID of the form `run_` + 32 hex characters (previously `run_` +
> 16). Native (non-imported) run IDs are unchanged.

## Detecting available integrations

To check which framework integrations are available in the current environment,
use detection. It reports whether each integration's SDK is importable and does
**not** instrument anything — it never wraps, patches, or registers a call.

```python
from agentloop import detect_integrations

result = detect_integrations()
# result.available   -> ["openai", ...]
# result.unavailable -> {"langgraph": "package not installed", ...}
```

Or from the CLI:

```bash
uv run agentloop detect-integrations
```

Detection uses `importlib.util.find_spec`, so it does not import the frameworks
and is safe and cheap to run at application startup. To actually record traces,
apply the helpers above (or the generic decorator SDK) from the application
process you want to trace.

> **Migration note.** `detect_integrations()` replaces `auto_instrument()`, and
> the CLI command `detect-integrations` replaces `auto-instrument`. The old name
> implied it enabled instrumentation, which it never did. `auto_instrument()`
> still works as a deprecated alias (it emits a `DeprecationWarning`), and the
> result fields were renamed from `enabled`/`skipped` to `available`/`unavailable`.

## Typical workflow

1. Install AgentLoop.
2. Instrument the framework already used by the team.
3. Run one production-like agent task.
4. Upload or store the trace.
5. Open the dashboard.
6. Use optimization cards to reduce latency and token spend.

These adapters let developers add AgentLoop without rewriting an agent around a
new framework or replacing its existing tracing stack.
