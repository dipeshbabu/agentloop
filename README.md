# AgentLoop

AgentLoop is a lightweight Python profiler for multi-step AI agent workflows. It traces model calls, tool calls, retries, token usage, repeated context, and latency bottlenecks, then produces concrete optimization recommendations.

## Install

```bash
pip install -e ".[dashboard,dev]"
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
```

## Run demo

```bash
python examples/research_agent_demo.py
agentloop report runs/research_agent_baseline.json
streamlit run dashboard/app.py
```

## Product path

Profiler -> Recommendations -> Optimizer -> Runtime -> Hosted AgentLoop Cloud
