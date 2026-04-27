# AgentLoop

AgentLoop is a profiler and optimization layer for AI agent execution graphs. It traces model calls, tool calls, retries, token usage, repeated context, and latency bottlenecks, then turns the trace into concrete workflow rewrite recommendations.

AgentLoop is not memory for agents. It is performance engineering for agent loops.

The product thesis is simple: agents do not execute like single prompt-response apps. They loop, call tools, retry, branch, summarize, and carry context. AgentLoop measures that loop and recommends how to restructure it.

## Install

```bash
pip install -e ".[all,dev]"
```

## Quickstart

```python
from agentloop import trace_agent, trace_model_call, trace_tool_call

with trace_agent("research_agent") as trace:
    with trace_model_call("plan", model="gpt-4.1", input_tokens=1200, output_tokens=200):
        pass
    with trace_tool_call("search_web"):
        pass

trace.print_report()
trace.export_json("runs/research_agent.json")
```

## Run the sellable demo

The CLI demos are package-internal, so they work even when the `examples/` directory is not importable as a Python package.

```bash
agentloop demo-all
agentloop compare
agentloop audit --out runs/audit.md
agentloop optimize --out runs/optimization_plan.md --json-out runs/optimization_plan.json
streamlit run dashboard/app.py
```

You can still run individual demos:

```bash
agentloop demo --kind baseline
agentloop demo --kind optimized
agentloop demo --kind langgraph
python examples/langgraph_auto_instrumentation_demo.py
```

`agentloop compare`, `agentloop audit`, and `agentloop optimize` auto-generate missing demo traces by default, so missing `runs/research_agent_baseline.json` should not block the local workflow.

## Optimization plans

```bash
agentloop optimize runs/research_agent_baseline.json --out runs/optimization_plan.md --json-out runs/optimization_plan.json
```

AgentLoop reconstructs an execution graph, identifies bottlenecks, and emits optimization cards such as:

- parallelize independent tool calls
- cache repeated prompt/context prefixes
- batch repeated model calls
- route small steps to cheaper models
- reduce retry loops with structured outputs
- split or compress oversized reasoning steps

Each card includes a reason, affected nodes, confidence, rewrite hint, and estimated latency or cost savings.

## Dashboard

```bash
streamlit run dashboard/app.py
```

The dashboard can generate demo traces directly from the UI and includes tabs for:

- run summary
- timeline
- execution graph
- optimization plan

## Local API server

```bash
agentloop server --host 127.0.0.1 --port 8000
```

Endpoints:

- `GET /health`
- `POST /traces`
- `GET /traces`
- `GET /traces/{run_id}/report`

This is the start of the hosted product path: SDK traces can be sent to an API, stored, and turned into reports.

## LangGraph usage

### Decorate individual nodes

```python
from agentloop import trace_agent
from agentloop.integrations.langgraph import trace_node

@trace_node("retrieve_sources")
def retrieve_sources(state):
    return state

with trace_agent("research_graph") as trace:
    retrieve_sources({})

trace.export_json("runs/research_graph.json")
```

### Auto-instrument a StateGraph builder

```python
from agentloop.integrations.langgraph import instrument_state_graph, trace_runnable

builder = StateGraph(State)
instrument_state_graph(builder)

builder.add_node("retrieve", retrieve)
builder.add_node("synthesize", synthesize)

app = trace_runnable(builder.compile(), name="research_graph")
app.invoke({"question": "What is agent profiling?"})
app.export_last_trace("runs/research_graph.json")
```

This integration is dependency-free. AgentLoop does not import LangGraph directly; it wraps StateGraph-like builders and compiled runnable-style objects.

## Product path

Profiler -> Execution graph -> Optimization cards -> Rewrite recommendations -> Hosted AgentLoop Cloud

See `docs/PRODUCT.md` for the go-to-market wedge.
