# Operation kinds

Use `metadata["operation_kind"]` to identify what a span does while retaining
the existing `event_type` category. This uses the schema's extensible metadata
object; traces remain schema 1.1 and existing 1.0 files need no migration.

| Kind | Meaning |
| --- | --- |
| `agent` | One agent invocation or agent lifecycle operation |
| `workflow` | Orchestration of multiple steps or agents |
| `model` | Inference, generation, or embedding computation |
| `tool` | Invocation of an application tool |
| `retriever` | Retrieval of relevant documents or records |
| `memory` | Reading, writing, or managing agent memory |
| `reranker` | Reordering retrieved candidates by relevance |
| `guardrail` | Enforcing a safety, validity, or budget constraint |
| `evaluator` | Evaluating output or task quality |
| `retry` | Repeating a failed or rejected step |

The framework-neutral `AgentEvent.operation_kind` property normalizes surrounding
whitespace and letter case. `ExecutionNode.operation_kind` exposes the same value.
The original label remains in metadata. With no label, `model_call` maps to
`model`, `tool_call` to `tool`, and `retry` to `retry`.

An unrecognized, empty, or non-string label is analyzed as `unknown`; its raw
metadata is retained. It does not silently inherit model or tool semantics.
Unknown legacy event types also analyze as `unknown`. Add application-specific
details under other metadata keys rather than inventing a near-synonym for a
supported kind.

## Capture and analysis

Existing tracing helpers accept metadata, so integrations need no framework
types or new dependency in the core:

```python
from agentloop import trace_agent, trace_tool_call

with trace_agent("search-workflow") as trace:
    with trace_tool_call("retrieve", metadata={"operation_kind": "retriever"}):
        documents = ["synthetic document"]

assert trace.events[0].event_type == "tool_call"
assert trace.events[0].operation_kind == "retriever"
assert trace.report()["operation_counts"] == {"retriever": 1}
```

Legacy model/tool/retry counts, token accounting, and elapsed-time metrics still
use `event_type` for compatibility. Reports add `operation_counts`; replay keeps
those counts separately for each condition. Graph nodes and finding evidence
carry the normalized kind next to each span ID. This is also available to study
consumers through the report and replay JSON.

Parallelization and tool-oscillation rules operate on `tool` spans, so an agent,
workflow, retrieval, or unknown operation does not receive tool-specific savings
merely because its legacy category is `tool_call`. Loop detection separates
groups by normalized kind and name. These rules are version 1.1; estimator
versions describe the prediction formulas independently.
Model and retry rules likewise require their normalized kinds. Repeated-context
analysis excludes spans explicitly labeled as another operation kind.

## OTLP mapping

The importer maps `gen_ai.operation.name` values as follows:

| Source operation | Normalized kind | Legacy category |
| --- | --- | --- |
| `chat`, `text_completion`, `generate_content`, `embeddings` | `model` | `model_call` |
| `execute_tool` | `tool` | `tool_call` |
| `invoke_agent`, `create_agent` | `agent` | `tool_call` |
| `invoke_workflow` | `workflow` | `tool_call` |
| `retrieval` | `retriever` | `tool_call` |
| `retry` | `retry` | `retry` |

These names follow the [OpenTelemetry GenAI attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).
Explicit operations take precedence over span-name hints. Unknown source
operations keep their original label in metadata and analyze as `unknown`.
If the source has no operation, the importer retains its legacy name-based
fallback and derives the kind from that category.

AgentLoop exports explicitly supplied labels through
`agentloop.operation_kind` and its metadata namespace. On import, native
metadata takes precedence, followed by that explicit attribute and then the
GenAI operation. Native `agentloop.event_type` takes precedence for the legacy
category, retaining both fields even when an application deliberately combines
them. Repeated native OTLP round trips preserve operation metadata without
adding a label to older, unlabeled native events.
