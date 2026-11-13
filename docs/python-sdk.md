# Python SDK

Wrap the operations you want to measure and export the final trace. This example
uses local data and needs no model provider.

```python
import agentloop


@agentloop.trace_tool(name="lookup_note")
def lookup_note(topic: str) -> str:
    return {"quality": "Check the final answer."}[topic]


with agentloop.trace_agent("notes_agent") as trace:
    output = {"note": lookup_note("quality")}
    trace.metadata["output"] = output

trace.export_json("runs/notes_agent.json")
```

Analyze the recorded operations and open the report.

```bash
agentloop analyze runs/notes_agent.json --html runs/notes_agent.html
```

## Tracing entry points

| Entry point | Purpose |
| --- | --- |
| `trace_agent(name)` | Open and finalize a trace context |
| `trace_tool(name=...)` | Decorate a sync or async tool function |
| `trace_model(name=...)` | Decorate a model operation |
| `trace_tool_call(name, ...)` | Record an explicit tool span |
| `trace_model_call(name, ...)` | Record an explicit model span |
| `record_model_call(...)` | Record a completed call with observed timing and usage |
| `AgentTrace.from_json(path)` | Read an existing native trace |
| `trace.export_json(path)` | Save a trace for analysis and comparison |

The [framework integration guide](INTEGRATIONS.md) includes provider and framework
examples. [Workflow tracing](WORKFLOW_TRACING.md) covers stages and structured
decision pipelines.

## Output and usage evidence

The example explicitly retains the final output in `metadata.output` so quality
checks can evaluate it. Choose the task's expected answer independently and keep
it in the [quality fixture](STRUCTURED_QUALITY.md) or guided task manifest.

Record token counts from their available source. Provider counts, tokenizer
counts, supplied counts, estimates and unavailable usage keep their provenance.
Read [trace schema](TRACE_SCHEMA.md) and [pricing](PRICING.md) before interpreting
cost comparisons. [Trace retention](TRACE_RETENTION.md) covers capture controls.

## Compare an applied change

Use [guided optimization](GUIDED_OPTIMIZATION.md) to freeze a task set and
baseline findings before capturing a candidate. The [intervention ledger](INTERVENTIONS.md)
links original predictions with outcomes, and [paired studies](STUDIES.md) retain
repeated comparisons.

## Clients and other runtimes

The [HTTP API guide](API_VERSIONING.md) describes API v1 and client compatibility.
The [TypeScript SDK](../sdk/typescript/README.md)
records native traces and provides an API client for Node.js applications.
