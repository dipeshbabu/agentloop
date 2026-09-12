# Telemetry conformance

AgentLoop imports OTLP JSON without an OpenTelemetry SDK, collector, or provider
account. The offline [fixtures](../tests/fixtures/telemetry) identify their upstream
revision and use synthetic values. Their field mappings follow:

- [OpenTelemetry GenAI/MCP development conventions at `0c875949`](https://github.com/open-telemetry/semantic-conventions-genai/tree/0c87594975195608dc91b3f702e250a7b240c151).
- [OpenInference conventions at `a332a5ca`](https://github.com/Arize-ai/openinference/blob/a332a5ca8f271356b48d450220e3cf364e45ccac/spec/semantic_conventions.md).

These are pinned development snapshots, not a claim that every upstream field or
future convention version is supported. MCP fixtures use protocol `2025-06-18`.

## Supported fields

| Source | AgentLoop representation |
| --- | --- |
| `traceId`, `spanId`, `parentSpanId` | Trace/event identity and graph parent edges, scoped to one source trace |
| Span `links`, including trace/span IDs, flags, trace state, and attributes | `event.metadata.otel_links`; retained as links, never assumed to be causal dependency edges |
| Span `kind`, `flags`, `traceState` | `otel_span_kind`, `otel_span_flags`, `otel_trace_state` metadata |
| Resource attributes, instrumentation scope, schema URL | `otel_resource_attributes`, `otel_scope`, `otel_schema_url` metadata |
| `gen_ai.operation.name` | [Normalized operation kind](OPERATIONS.md), including agent/workflow/retrieval and current memory operations |
| `openinference.span.kind` | LLM/EMBEDDING → model; CHAIN → workflow; AGENT, TOOL, RETRIEVER, RERANKER, GUARDRAIL, EVALUATOR → corresponding kinds |
| `mcp.method.name` | `tools/call` → tool; `resources/read` → retriever; other MCP methods retain their attributes and use unknown kind |
| Response/request model attributes | Response model takes precedence for pricing, followed by request model; OpenInference model-name and embedding-model aliases are accepted |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | Input/output token counts; OpenInference `llm.token_count.prompt/completion` and older `llm.usage.*` aliases are supported |
| Cache-read, cache-write, reasoning usage | `cached_input_tokens`, `cache_write_input_tokens`, `reasoning_output_tokens` metadata, with OpenInference detail aliases |
| `gen_ai.provider.name`, older `gen_ai.system`, `llm.provider` | Provider metadata |
| `llm.cost.total` | Finite, nonnegative reported USD cost; invalid values remain raw metadata without overriding calculated pricing |
| `gen_ai.conversation.id`, `session.id`, `mcp.session.id` | Conversation/session metadata |
| `gen_ai.tool.call.id`, `tool.id`, `tool_call.id` | Tool-call identity metadata; request IDs remain separate |
| Numeric or symbolic OTLP error status, status description, exception events | Event status/error; exception and error-class attributes remain available |
| GenAI evaluation results, OpenInference evaluation/annotation attributes | `evaluation_results` metadata containing source, name, score, label, explanation, and source-defined interpretation |

Primary usage fields take precedence even when explicitly zero. Negative,
fractional, boolean, or malformed token counts raise `TraceValidationError`
instead of being truncated or reclassified as measured usage. No usage fields
means unavailable usage. Native legacy provenance remains unspecified through
export/import, rather than being upgraded to provider counts.

Cache and reasoning counts describe subsets of input/output usage and are not
added to those totals. Cache-write usage is preserved for analysis; pricing does
not invent a cache-write rate. Unknown modalities and usage extensions remain
in metadata.

## Evaluation logs and trace relationships

The importer accepts span payloads and optional OTLP `resourceLogs → scopeLogs →
logRecords` in the same JSON document. It associates logs by both trace ID and
span ID; logs without a matching span stay on the corresponding trace metadata.
Logs with no execution spans produce a trace with zero execution events and
`execution_data_present: false`, preserving the evidence without inventing work.

Raw log records and their resource/scope are retained in `otel_log_records`.
Evaluation attributes from logs or span events additionally appear in
`evaluation_results`. Their scale, scorer, and acceptance criteria belong to the
source. A score of 4 is not normalized into AgentLoop's `[0, 1]` quality range or
promoted into a passing replay gate. Configure quality fixtures separately.

MCP parent IDs preserve client → transport/server → tool relationships when
provided by the source. CLIENT/SERVER span kinds, MCP session/protocol fields,
JSON-RPC request IDs, and network attributes remain available. Import does not
create missing propagation, merge distinct traces, or deduplicate client and
server observations of the same request. Legacy counts still count spans.

## Preservation and known gaps

Unknown attributes are retained in event metadata. Native metadata wins a
collision with an external alias. OTLP arrays and key/value lists retain their
structure; byte values are wrapped as `{"otel_bytes_base64": "..."}` and
unrecognized AnyValue variants as `{"otel_unrecognized_value": ...}`. These
opaque representations are not interpreted as execution measurements.

Native exports preserve trace metadata, elapsed time, event durations, IDs,
provenance, nested metadata, links, span events, flags, and errors over repeated
round trips. Transport-only bookkeeping does not grow a nested namespace.
Imported log records survive in native metadata; this exporter does not emit a
separate OTLP Logs request. Original resource/scope values remain metadata on
export; AgentLoop's own resource identifies the exported trace.

OpenInference PROMPT, GenAI plan/fetch-response, and unknown kinds have no matching
operation in the current taxonomy and remain unknown. They are not treated as
inference merely because their name contains a model. Indexed OpenInference
annotation collections and unsupported telemetry extensions remain raw attributes.
This suite does not implement a collector, exporter transport, full OTLP schema
validation, or automatic conversation/task-quality aggregation.

Raw span attributes, events, and imported logs can contain prompts, outputs,
identifiers, or tool arguments. Import preserves supplied data; it does not redact
it. Review those fields before sharing exported research artifacts. Native
`input_text`/`output_text` fields are not automatically emitted as GenAI content
attributes.

Run the offline contracts with:

```bash
uv run --all-extras python -m pytest tests/test_telemetry_conformance.py tests/test_otel_interop.py tests/test_operations.py tests/test_token_provenance.py -q
```
