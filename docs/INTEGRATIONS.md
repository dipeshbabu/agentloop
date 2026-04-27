# AgentLoop Framework Integrations

AgentLoop's product wedge is low-friction instrumentation. A team should be able to wrap an existing production agent and immediately see latency, token, retry, and tool-call waste.

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

This is the easiest onboarding story for early customers: add three decorators, run the agent once, and get a trace.

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

## Product usage pattern

1. Install AgentLoop.
2. Instrument the framework already used by the team.
3. Run one production-like agent task.
4. Upload or store the trace.
5. Open the dashboard.
6. Use optimization cards to reduce latency and token spend.

This is the path from library to sellable product: framework integrations create distribution because developers do not want to rewrite their agents just to observe them.
