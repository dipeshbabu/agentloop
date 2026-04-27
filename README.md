# AgentLoop

AgentLoop is a profiler and optimization layer for multi-step AI agent workflows. It traces model calls, tool calls, retries, token usage, repeated context, and latency bottlenecks, then produces concrete recommendations.

The product thesis is simple: agents do not execute like single prompt-response apps. They loop, call tools, retry, branch, summarize, and carry context. AgentLoop measures that loop.

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
streamlit run dashboard/app.py
```

You can still run individual demos:

```bash
agentloop demo --kind baseline
agentloop demo --kind optimized
agentloop demo --kind langgraph
```

`agentloop compare` and `agentloop audit` auto-generate missing demo traces by default, so missing `runs/research_agent_baseline.json` should not block the local workflow.

## Dashboard

```bash
streamlit run dashboard/app.py
```

The dashboard can now generate demo traces directly from the UI, so a fresh clone no longer opens to an empty unusable state.

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

## What AgentLoop detects

- repeated context that should be cached or summarized
- sequential tool calls that should be parallelized
- retry waste caused by bad schemas or brittle parsing
- expensive model steps that should be split, compressed, or routed to smaller models
- latency split across model time, tool time, and retry time

## LangGraph-style usage

```python
from agentloop.integrations.langgraph import trace_node

@trace_node("retrieve_sources")
def retrieve_sources(state):
    return state
```

The integration is dependency-free: it wraps normal node functions and records them inside an active `trace_agent(...)` context.

## Product path

Profiler -> Audit reports -> Recommendations -> Optimizer -> Hosted AgentLoop Cloud

See `docs/PRODUCT.md` for the go-to-market wedge.
