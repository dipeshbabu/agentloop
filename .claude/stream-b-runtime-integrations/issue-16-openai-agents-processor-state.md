# Issue #16 — Isolate and release OpenAI Agents processor state per trace

- **Priority:** P1 · **Effort:** M · **Labels:** bug, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/16

## Problem

`AgentLoopTracingProcessor` stores every span in one **process-wide** list, never removes
completed traces/spans, and includes any span without a readable trace ID in **every** trace
that ends. Result: sequential traces leak unrelated events, concurrent traces corrupt each
other, memory grows for the process lifetime, and sensitive content lingers.

## Reproduction

One span without `trace_id`, then two different trace endings → the same span is exported
into **both** traces and stays retained (`retained_spans: 1`).

## Key files

- `agentloop/integrations/openai_agents.py:21-22` — `_traces` / `_spans` process-wide lists.
- `:39-40` — `on_span_end()` only appends.
- `:48-55` — `_build_trace()` scans full list; accepts `_span_trace_id(span) in {None, trace_id}`.
- `on_trace_end()` — exports but never cleans up.

## Approach

1. **Key spans by trace ID.** Store spans in a per-trace structure (`dict[trace_id, list]`)
   populated as spans arrive, instead of one global list.
2. **Bounded handling for unassociated spans.** If the SDK can omit trace IDs, define an
   explicit bounded association strategy (e.g. attach to the current active trace only) —
   never copy an unassociated span into every trace.
3. **Release on export.** `on_trace_end()` builds from that trace's spans, exports, then
   drops the trace's state. Memory must not grow with number of completed traces (except a
   documented, optionally-retained `exported_traces`).
4. **Define lifecycle edge cases:** duplicate `on_trace_end`, missing start/end callbacks,
   shutdown, force-flush.

## Acceptance criteria (from the issue)

- [ ] Interleaved traces export only their own spans.
- [ ] Completed trace and span state is released promptly.
- [ ] Unassociated spans have documented bounded handling and cannot be copied into multiple traces silently.
- [ ] Duplicate `on_trace_end`, missing start/end callbacks, shutdown, and force-flush behavior are defined.
- [ ] Tests cover sequential, concurrent, interleaved, incomplete, and high-volume trace lifecycles.
- [ ] Processor memory does not grow with completed traces except intentionally retained `exported_traces` (with a documented retention option).

## Testing

- `tests/` (new/adjacent to existing agents tests): sequential, concurrent, interleaved,
  incomplete (missing callbacks), and high-volume; assert per-trace isolation and that
  retained state is bounded after export.

## Compatibility / risk

- If any consumer relies on the current `exported_traces` growth, give it an explicit
  retention option and document the default.
