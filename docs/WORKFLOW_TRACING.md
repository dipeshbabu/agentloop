# Trace AI workflows and decision pipelines

`trace_workflow` profiles classifier/rule/retrieval/transform pipelines using the
same lifecycle, native trace schema, stores and HTTP routes as `trace_agent`.
`ExecutionTrace` is an alias of `AgentTrace`. Existing agent APIs and default
agent reports remain supported.

AgentLoop records the application's work and caller-declared outcomes. The host
application owns classification, routing and execution; this API adds no graph
runner, queue, scheduler or automatic policy enforcement.

## Try the offline pipeline

```bash
uv run python examples/workflow_trace.py --out runs/workflow-trace
uv run python examples/workflow_trace.py --linear --out runs/workflow-trace-linear
```

The [example](../examples/workflow_trace.py) profiles synthetic functions and
writes native JSON, analysis JSON, HTML and OTLP. It verifies metadata round trips
without retaining the input message. These are contract fixtures, not application
usefulness or performance evidence. A branching dependency declaration describes
dataflow; the example's application code still controls execution order.

## Declare workflow and stage identities

```python
from agentloop import (
    StageInfo,
    WorkflowInfo,
    set_workflow_outcome,
    trace_operation,
    trace_workflow,
)

with trace_workflow(
    "Message routing",
    workflow=WorkflowInfo(
        "message-router",
        "git:reviewed-revision",
        input_schema_ref="schema:message:1",
        output_schema_ref="schema:route:1",
        input_ref="task:message-123",
    ),
    task_id="message-123",
    example_id="routing-case-123",
) as trace:
    with trace_operation(
        "Classify",
        kind="classifier",
        stage=StageInfo("classify", "classifier-config-v3"),
        depends_on=[],
    ) as classification:
        # category = application_classifier(message)
        category = "billing"  # Placeholder for the application's result.
        classification.set_outcome(category, output_ref="result:category-123")

    with trace_operation(
        "Route",
        kind="rule",
        stage=StageInfo("route", "routing-rules-v2"),
        depends_on=[classification.event_id],
    ) as routing:
        # application_route(category)
        routing.set_outcome("routed", output_ref="result:route-123")

    set_workflow_outcome(trace, "routed", output_ref="result:route-123")
```

`WorkflowInfo` and `StageInfo` are immutable declarations. Both carry identity,
version and optional `input_schema_ref`, `output_schema_ref`, `input_ref` and
`output_ref`. A stage can declare a logical `kind` separately from the physical
operation. References are opaque strings: recording one does not retrieve it,
validate its schema or prove that an artifact exists. Hashes can be references;
predictable hashes are not anonymization.

Workflow task/example IDs also live in ordinary trace metadata for existing study
pairing. Explicit arguments accept strings or integers and reject conflicting
metadata. These APIs require and capture no raw input/output bodies. Metadata is
caller-provided JSON; use references and safe labels for shareable artifacts.

## Operations and resource scope

The existing taxonomy now also recognizes `rule`, `classifier`, `transform` and
`external_service`, alongside `model`, `tool`, `retriever`, `memory`, `reranker`,
`guardrail`, `evaluator`, `workflow`, `agent` and `retry`.

| Kind | Native event category | Accounting |
| --- | --- | --- |
| `model` | `model_call` | Existing model/token/cost metrics |
| `tool` | `tool_call` | Existing tool-call metrics |
| `retry` | `retry` | Existing retry metrics |
| Other kinds | `operation` | Operation counts and timing, without invented tool/model calls |

For a physical model call used for classification, use `kind="model"` and
`StageInfo(kind="classifier", ...)`. Both token counts are required for exact
provenance. Missing usage stays unavailable; non-model token usage is rejected
before a context body runs.

For dynamic SDK usage, keep existing model instrumentation and attach
`operation_metadata("model", stage=StageInfo(...))`, or call `record_operation`
after receiving normalized usage. Generic contexts do not inspect SDK responses.
Keep grouping spans distinct from physical model calls to avoid double counting.
Cost and token totals retain their **recorded model-call** scope; arbitrary
service billing and non-model computation are not included in those totals.

## Callbacks, status and outcomes

`record_operation` accepts completed timing, native `ok`/`error` status, optional
model usage, stage/reference metadata, dependencies and an explicit captured
`trace`/`parent_id`. An explicit trace does not inherit an unrelated ambient parent.
Records use the same `AgentEvent` validation and storage path as other traces.

`operation_metadata` and `workflow_metadata` support integrations that already own
their tracing lifecycle, without framework imports. `workflow_metadata` accepts
an explicit execution status for completed host observations.

Contexts restore trace/parent bindings and propagate original errors and
cancellation. Error class names are recorded; raw exception details require
`capture_error_detail=True`. `OperationSpan.set_outcome` accepts a bounded label
and optional output reference; its saved result cannot be rewritten after close.

Workflow status is `running`, then `completed`, `failed`, `cancelled` or
`interrupted`; metadata-only integrations may use `unknown`. Completion means the
traced scope returned, not that task quality passed. Keep worker lifetimes inside
the owning trace, entering/exiting contexts in the same execution context. For
streams, use completed-span records with captured context or an existing adapter
that restores context on each resume.

## Dependencies and compatibility

Dependencies reference span/event IDs, not stage names. Omission is undeclared;
`[]` explicitly declares a root. Parent spans describe containment. Dependencies
describe prerequisites and do not establish read/write safety or permission to
parallelize.

When explicit stage dependencies are present, graphs use parent and declared
edges instead of inventing causal edges from timestamps. Declaration coverage,
invalid references, unresolved IDs and cycles remain visible. Invalid/unresolved
declared graphs have no critical-path duration; direct `critical_path()` raises
an explicit error. Validation reflects graph mutations. Without declarations,
the existing inferred-sequence behavior remains identified as such. Legacy agent
graphs retain their behavior.

Ordinary workflow repetition does not activate agent-specific runaway-loop or
tool-oscillation heuristics. Other findings keep their assumptions and gain stage
context in evidence; a finding remains a hypothesis requiring validation.

No native schema or storage migration is needed. Versioned `agentloop.workflow`
and `agentloop.stage` metadata carry the declarations. Unknown kinds remain intact
and analyze as `unknown`; unknown metadata versions remain in native artifacts
and appear as unsupported in projections.

Graphs, analysis/findings, replay, studies, native JSON, existing stores/HTTP routes,
HTML/Markdown and dashboard views preserve the metadata. Studies cannot infer
success for known failed/cancelled/interrupted workflows merely from absent error
spans; unfinished/unknown completion is indeterminate. Completed execution without
task evidence still does not establish task quality.

OTLP carries workflows on resource metadata and stages on spans. Generic
operations use native AgentLoop model/usage attributes rather than claiming GenAI
operations. Physical legacy model/tool/retry exports keep their conventions.
Original event IDs preserve dependency references through repeated round trips.
