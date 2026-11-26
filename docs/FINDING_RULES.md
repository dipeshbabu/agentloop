# Canonical finding rules

The [investigation ranking contract](FINDING_RANKING.md) preserves canonical
predictions while exposing evidence, quality risk and validation effort. Rule
authors can declare required ranking inputs without adding domain logic to the
ranking engine.

The [finding trust benchmark](FINDING_BENCHMARKS.md) evaluates versioned rules
against independently labeled fixtures, preserving false positives, abstentions,
unknown labels and backend provenance in a release regression gate.

`agentloop.rules` owns built-in detection. A `FindingRule` has a stable `rule_id`,
a version, and a detector accepting `AnalysisContext` (metrics plus execution
graph). It returns `FindingCandidate` objects containing wording, confidence,
affected spans, estimates, and available evidence/assumptions.

The registry has a fixed order: parallelization, context caching, schema/retry
validation, batching, routing, large steps, runaway loops, and tool oscillation.
Rule versions are `1.0` for the initial registry. Change a version when changing
a rule's interpretation or detection semantics. There is no remote rule loading,
plugin discovery, or executable rule configuration in the HTTP API.

Reports expose `finding_candidates` and derive `recommendations` from them.
Optimization consumes those same candidates instead of applying another set of
thresholds. Callers supplying an older report without candidates use the same
registry as a fallback. Diagnosis, patch planning, CLI, dashboard, API, and CI
therefore use the same finding types and wording. For example, report context
caching now uses the optimizer's title, `Cache repeated prompt/context prefix`.

## Schema repair eligibility

`add_schema_validation` version `1.2` requires a retry span with explicit
`retry_reason: invalid_output_schema` or `reason: schema_validation_failed`
metadata. The more specific `retry_reason` field takes precedence when both
are present. Transport failures, rate limits and missing or unknown causes
remain visible in retry counts and durations. Schema recommendations and their
heuristic savings cover only the spans with declared schema failures.

These declarations identify the recorded cause. The recommendation retains
medium confidence, and correctness must be verified using the developer's
independent task checks. Existing traces remain readable. Older traces with
unclassified retry spans can produce fewer schema-repair recommendations.

## Batching eligibility

`batch_model_calls` version `1.2` groups at least three calls with the same name,
model identity, and parent scope. It excludes groups where a call depends on
another group member, including dependencies through tools or other spans.
Native `depends_on` metadata, declared workflow-stage dependencies, explicit
graph dependency edges, and parent relationships all contribute to this check.
Missing references, malformed declarations, duplicate span IDs, and cyclic
ancestry prevent recommendations for affected groups. A shared external input
does not by itself make the calls dependent on one another.

Inferred sequence edges describe observed order and are not treated as causal
dependencies. Traces without dependency declarations can still produce batching
hypotheses; verify output independence, shared-state safety, and task quality
before applying them. This rule does not infer hidden dependencies from prompts
or tool outputs, and it does not rewrite historical findings or study evidence.

## Model routing needs a quality check first

`route_to_smaller_model` version `1.3` still flags small model steps, with the
same thresholds and estimate. Its title now reads "Check quality before routing
`step` to a smaller model", and its rewrite hint says a quality check is required
before switching. The rule also declares the ranking requirement
`smaller_model_quality_check`, which is a quality prerequisite: until it is
declared `passed`, the finding ranks after every other finding
(see [quality prerequisites](FINDING_RANKING.md#quality-prerequisites)).

The reason is the [November 2026 optimization benchmark](OPTIMIZATION_BENCHMARK.md).
This finding ranked first on all three agents, and applying it cut answer quality
on 31 of 75 held-out tasks. Its uncalibrated estimate of a 25% time saving was
also too low: 50-73% was measured. Token counts and model size say nothing about
whether a smaller model gets the task right.

The finding is kept, and its ID still uses the version `1.2` title, so reviewed
lifecycle decisions stay attached. The change was not evaluated on the
benchmark's held-out tasks, because they informed it.

## Failures and completeness

Each detector failure is isolated. Other rules still return their findings, and
`rule_errors` records the failed ID/version, a safe error category, and a generic
message. Raw exception messages and implementation class names are not serialized
into reports or HTTP responses. For local debugging, enable DEBUG logging for
`agentloop.rules`; detailed exception logs may contain private inputs and should
remain restricted to operators.
Reports, plans, and diagnoses mark `analysis_complete: false`; human reports and
dashboard views show the diagnostics. CI fails incomplete diagnosis even when its
performance gates pass, so missing analysis cannot produce a clean merge signal.

When persisting an incomplete diagnosis, returned findings may be updated but
missing historical findings are not superseded. A failed rule cannot establish
that its earlier findings disappeared. A later complete diagnosis applies the
normal lifecycle rules.

## Compatibility

`agentloop.optimizer.OptimizationCard` and `RecommendationType` remain available
as compatibility imports. Finding IDs keep the existing type/title/span fingerprint;
adding rule IDs or versions does not itself replace historical identities. Built-in
detector thresholds, savings calculations, and ordering are retained, as are
titles apart from the `route_to_smaller_model` 1.3 wording above;
report recommendation wording now follows the canonical detector rather than the
old separate report-only summaries. Rule metadata is additive in JSON and persists
with findings; no database migration is required.
