# Parallelization evidence

Three or more same-named tool calls in the same parent scope can suggest a
concurrency experiment. Names and ordering do not establish that their outputs
or shared state are independent. AgentLoop reports a candidate with explicit
assumptions and validates improvements through baseline/candidate replay.

## Evidence levels

- `observed`: recorded call counts, cumulative span durations, and reliable timing.
- `declared`: the integration or user states that every selected call is safe to
  execute concurrently by setting `metadata.parallel_safe` to the boolean `true`.
- `inferred`: the structural heuristic suggests a candidate without that declaration.
- `verified`: a measured intervention supports an effect. This detector does not
  assign this level; a repeated-name pattern or declaration is never verification.

Candidates, optimization cards, and persisted findings expose `evidence_level`,
`assumptions`, and `observations`. Unconfirmed candidates have low confidence;
declared candidates with reliable timing have medium confidence. Reports,
Markdown exports, patch guidance, and dashboard views retain the qualifications.

## Optional dependency metadata

The existing event `metadata` object accepts these hints without a trace-schema change:

```json
{
  "parallel_safe": true,
  "depends_on": ["input-span-id"]
}
```

`depends_on` is a list of span IDs in the same trace. Direct or transitive
dependencies between candidate calls block the suggestion, including dependencies
through model or other non-tool spans. A common external input does not by itself
establish an inter-call dependency. Graphs constructed in Python can also supply
edges with kind `dependency` or `depends_on`; inferred `sequence` edges are temporal
ordering hints and are not treated as causal proof.

Explicit dependency conflicts, `parallel_safe: false`, invalid declarations,
malformed dependency lists, and references to missing dependency spans exclude the
affected group. Declarations cannot override these conflicts. Omit `parallel_safe`
when safety is unknown; a declaration on only some calls leaves the group inferred.

## Savings assumptions and limits

The estimated parallel time is the maximum selected call duration. Estimated
savings are the sum of selected durations minus that maximum, assuming:

- no call consumes another selected call's output;
- calls do not conflict through shared mutable state;
- the calls are on the runtime-limiting path and scheduling adds no overhead.

Groups with reliably observed overlap are excluded because they are already
executing concurrently. Partially overlapping groups are conservatively excluded
too. Legacy traces with missing or inconsistent timestamps retain a low-confidence
candidate and an additional assumption that the calls ran serially. The legacy
`sequential_time_ms` field is the sum of span durations, not a claim that serial
execution was measured. Estimated savings are hypotheses, not measured speedups.

Existing titles and finding IDs remain stable when the selected spans are unchanged.
Groups now excluded by dependency, scope, or timing evidence can become superseded
on explicit re-diagnosis under the existing finding lifecycle.
