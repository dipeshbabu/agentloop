# Optimization evidence roadmap review

Reviewed on 2026-09-30 for [roadmap #208](https://github.com/dipeshbabu/agentloop/issues/208),
through [PR #260](https://github.com/dipeshbabu/agentloop/pull/260). All eleven
workstreams have implementation and validation evidence on main. These remain
**Unreleased source-checkout capabilities**; this review does not publish a new
PyPI version or activate an optimization in production.

## Implementation and evidence

| Workstream and merged PR | Supported behavior and validation |
| --- | --- |
| #209 — [#240](https://github.com/dipeshbabu/agentloop/pull/240) | [Finding-quality benchmarks](FINDING_BENCHMARKS.md) retain exact family/span labels, false positives, ambiguous/unknown cases, abstentions, evidence completeness and group splits. |
| #210 — [#251](https://github.com/dipeshbabu/agentloop/pull/251) | [Evidence-aware ranking](FINDING_RANKING.md) keeps risk, reversibility, effort, cost and evidence readiness explicit; unknown inputs do not inherit high priority from estimated savings. |
| #211 — [#252](https://github.com/dipeshbabu/agentloop/pull/252) | [Native optimization experiments](OPTIMIZATION_EXPERIMENTS.md) freeze original predictions and cases, journal bounded candidate attempts and export quality/intervention/study evidence without automatic rollout. |
| #212 — [#253](https://github.com/dipeshbabu/agentloop/pull/253) | [Context and reuse experiments](CONTEXT_REUSE_EXPERIMENTS.md) preserve protected content, versioned cache keys, invalidation, fallback and missing usage/quality evidence. |
| #213 — [#254](https://github.com/dipeshbabu/agentloop/pull/254) | [Structural experiments](STRUCTURAL_EXPERIMENTS.md) cover stage removal, conditional routing, bounded batches and declared safe parallelism, retaining downstream and partial failures. |
| #214 — [#255](https://github.com/dipeshbabu/agentloop/pull/255) | [Versioned retention](TRACE_RETENTION.md) records sampling/protection decisions, preserves original supported totals and identities, omits payloads by default and rejects replay/study claims from missing evidence. |
| #215 — [#256](https://github.com/dipeshbabu/agentloop/pull/256) | [Incremental aggregation](INCREMENTAL_AGGREGATION.md) uses bounded histograms/candidates, explicit completeness, compatible partition merges and streaming file/manifest CLI inputs. |
| #216 — [#257](https://github.com/dipeshbabu/agentloop/pull/257) | [Onboarding](ONBOARDING.md) reuses standards imports and SDK adapters, validates capture gaps before analysis and retains an actual local adapter/import overhead measurement. |
| #217 — [#258](https://github.com/dipeshbabu/agentloop/pull/258) | [Drift reports](DRIFT.md) compare immutable reviewed windows/rules, expose cohort denominators and confounders, and retain missing, small-sample, noisy and incomparable outcomes. |
| #218 — [#259](https://github.com/dipeshbabu/agentloop/pull/259) | [Cross-workload benchmark](USEFULNESS_BENCHMARK.md) separates 222 reference comparisons from 42 historical real-agent pairs, retains six unmatched onboarding slots and publishes reproducible finding/resource/quality results. |
| #219 — [#260](https://github.com/dipeshbabu/agentloop/pull/260) | [Real non-agent study](NON_AGENT_STUDY.md) retains 480 fitting/held-out comparisons across learned CPU classification, matching and conditional fallback, including negative candidates and native finding/calibration feedback. |

## Product workflow review

The implemented path connects native/standards-compatible traces and aggregates
to qualified findings, explicit investigation ranking, frozen candidate experiments,
independent quality/performance evidence and reviewed baseline monitoring. Existing
intervention records, paired studies, reports, scorer contracts and conformance
suites remain the shared foundations. Where supported, guarded activation follows
the separately reviewed [policy-promotion contract](POLICY_PROMOTION.md); candidate
generation and a successful report do not authorize deployment.

| Roadmap principle | Review finding |
| --- | --- |
| Prefer abstention to weak findings | Unknown/ambiguous labels and incomplete evidence remain visible. The cross-workload corpus exposes six repository-agent batching false positives; the review does not claim universal recommendation precision. |
| Keep quality ahead of savings | Cheaper real-model candidates retain quality losses. Non-agent batching preserves predictions, yet bean cases below the absolute quality floor remain rejected. |
| Separate evidence kinds | Synthetic reference accounting, actual CPU/local-model observations, original predictions, judged evidence, calculated costs and unknown operating costs are identified separately. |
| Preserve original evidence | Prediction/selection journals precede new candidates. Historical predictions are not regenerated to claim old effects. Both studies restore and reconstruct from checksum-checked archives. |
| Support scale without raw payload retention | Retention and bounded aggregates preserve supported original metrics while identifying absent span/quality evidence. Sampling prefix counts are not summed into a fabricated population. |
| Keep host control | Instrumentation requires an explicit startup call or existing telemetry. Experiments, drift comparisons and research reports invoke no production rollout, notification or autonomous rollback. |
| Retain failures and uncertainty | Failed, rejected, unmatched, unknown-cost and indeterminate cases remain in denominators. Task-cluster intervals do not treat repeated runs as independent tasks. |
| Validate implementation changes | Each implementation PR had green branch checks before creation and all PR checks before squash merge. Latest implementation validation passed 2,108 Windows tests with 106 documented skips; CI covers its configured Python/platform/service matrices. |

## Remaining limits

The cross-workload and non-agent studies are exploratory public-data research,
not production traffic or vendor rankings. Numeric CPU models have no meaningful
language-token usage and unmeasured operating cost. The observed recorder overhead
is material for short calls; recording-disabled timings are retained separately.
Operator minutes were not timed, even where setup steps are enumerated.

Only compatible isolated interventions enter per-finding calibration. Combined
configuration changes retain their ledgers and quality outcomes without assigning
the same effect to individual findings. No fitted coefficient is installed.
Drift thresholds are reviewed tolerances; histogram intervals and deadbands are
not universal anomaly detection or proof of task correctness.

Hashes detect changes and support references; they are not anonymity or
authentication. Data/scorer/model permissions and real rollout approvals remain
host responsibilities. New workloads, changed versions and broader claims require
new evidence. The documented limits are preserved in the artifacts and do not
prevent using the implemented workflow within its stated scope.
