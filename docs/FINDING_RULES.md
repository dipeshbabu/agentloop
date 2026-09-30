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
detector titles, thresholds, savings calculations, and ordering are retained;
report recommendation wording now follows the canonical detector rather than the
old separate report-only summaries. Rule metadata is additive in JSON and persists
with findings; no database migration is required.
