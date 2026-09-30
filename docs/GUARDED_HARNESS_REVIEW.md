# Guarded harness implementation and evidence review

Reviewed on 2026-09-30 for [roadmap #182](https://github.com/dipeshbabu/agentloop/issues/182),
through [PR #249](https://github.com/dipeshbabu/agentloop/pull/249). The core controls,
empirical evaluation and deferred follow-ups are implemented. These APIs remain
**Unreleased source-checkout features**; this review does not publish version 0.8.
The existing 0.7 evidence workflow remains the released baseline for its
[real-agent pilot](REAL_AGENT_STUDY.md).

## Implementation coverage

| Roadmap item and merged PR | Supported scope and evidence |
| --- | --- |
| #183 — [#221](https://github.com/dipeshbabu/agentloop/pull/221) | Opt-in [Python lifecycle controls](HARNESS.md), explicit capabilities and disabled/shadow/enforce modes; [lifecycle tests](../tests/test_harness.py). |
| #184 — [#222](https://github.com/dipeshbabu/agentloop/pull/222) | [Native decisions and intervention links](HARNESS_EVIDENCE.md), configuration hashes, actual resolution and overhead; [evidence tests](../tests/test_harness_evidence.py). |
| #185 — [#223](https://github.com/dipeshbabu/agentloop/pull/223) | [Atomic admission budgets](BUDGETS.md), reservations, usage provenance and concurrent accounting; [budget tests](../tests/test_budgets.py). |
| #186 — [#224](https://github.com/dipeshbabu/agentloop/pull/224) | [Bounded retries and caller-declared progress](LOOP_GUARDS.md), explicit side-effect safety; [loop tests](../tests/test_loop_guards.py). |
| #187 — [#225](https://github.com/dipeshbabu/agentloop/pull/225) | One pinned [LangGraph adapter](LANGGRAPH_HARNESS.md); [shared enforcement](../tests/test_enforcement_conformance.py) and [real SDK tests](../tests/test_langgraph_harness.py). |
| #188 — [#226](https://github.com/dipeshbabu/agentloop/pull/226), [#242](https://github.com/dipeshbabu/agentloop/pull/242) | Frozen [ablation protocols](HARNESS_ABLATIONS.md) and a [real SQL-agent ablation](REAL_HARNESS_STUDY.md), with original observations and independent quality. |
| #189 — [#227](https://github.com/dipeshbabu/agentloop/pull/227), [#243](https://github.com/dipeshbabu/agentloop/pull/243) | [Offline estimator calibration](CALIBRATION.md) and [empirical calibration](EMPIRICAL_CALIBRATION.md), including abstentions, failures and held-out diagnostics. |
| #190 — [#244](https://github.com/dipeshbabu/agentloop/pull/244) | Explicit [context transformations](CONTEXT_CONTROLS.md), protected results and shared summary budgets; [paired context study](CONTEXT_STUDY.md). |
| #191 — [#245](https://github.com/dipeshbabu/agentloop/pull/245) | Allowlisted [model routing](MODEL_ROUTING.md), capability checks and bounded fallback; [routing study](ROUTING_STUDY.md). |
| #192 — [#246](https://github.com/dipeshbabu/agentloop/pull/246) | [Declared tool scheduling](TOOL_SCHEDULING.md), bounded concurrency and retained partial work; [SQL scheduling study](SCHEDULING_STUDY.md). |
| #193 — [#247](https://github.com/dipeshbabu/agentloop/pull/247) | [Versioned completion checks](COMPLETION.md), bounded repair, deadlines and untrusted feedback handling; [completion tests](../tests/test_completion.py). |
| #194 — [#248](https://github.com/dipeshbabu/agentloop/pull/248) | Narrow [host checkpoint recovery](CHECKPOINT_RECOVERY.md), atomic claims, lineage and carried quotas; [protocol tests](../tests/test_checkpoint_recovery.py) and [pinned SDK tests](../tests/test_langgraph_recovery.py). |
| #195 — [#249](https://github.com/dipeshbabu/agentloop/pull/249) | [Reviewed budget-policy promotion](POLICY_PROMOTION.md), explicit opt-in, bounded canaries, version invalidation and rollback; [promotion tests](../tests/test_policy_promotion.py). |

## Roadmap gates

| Gate | Review result |
| --- | --- |
| Ordinary tracing remains compatible | Harnesses default to disabled. Existing [integration conformance](../tests/test_integration_conformance.py), [telemetry conformance](../tests/test_telemetry_conformance.py), [context](../tests/test_trace_context.py) and [trace-schema](../tests/test_trace_schema.py) suites remain in CI. |
| Denied work does not dispatch | Shared custom-Python/LangGraph conformance covers denial, cancellation and lifecycle handling. Each advanced adapter validates its declared coverage; unsupported paths fail explicitly. |
| Limits have accurate guarantees | Budgets, scheduling, completion and recovery document cooperative admission, unknown usage and already-running work. There is no process sandbox, forced thread termination, provider billing cap or exactly-once side-effect guarantee. |
| Actions preserve provenance | Native decision records retain policy/config versions, mode, resolution, call linkage and evaluation timing. Adapter receipts and audit exports retain failed attempts. Raw arguments/outputs are excluded by default; explicitly supplied identifiers and content hashes still need appropriate handling. |
| Evaluation preserves losses and uncertainty | Frozen plans, original predictions, held-out tasks and source-linked artifacts retain failures, early stops, missing pairs, quality losses and unknown cost. Scoring remains independent of a policy's own completion checks. |
| Activation is reviewed and reversible | The supported promotion family requires eligible empirical evidence, authenticated host/operator approval, explicit scope and a bounded canary. Kill switches and regression/expiry/version checks restore the reviewed baseline. No studied candidate is activated by this review. |
| Changes pass repository validation | Implementation PRs include tests and compatibility notes. Final #249 checks passed after correcting an existing synthetic timing fixture. A skipped CodeRabbit review was recorded as skipped, not counted as a completed review. |

## What the measurements establish

The [real-agent pilot](REAL_AGENT_STUDY.md) and [routing follow-up](ROUTING_STUDY.md)
retain faster smaller-model candidates that lose answer quality. The
[harness ablation](REAL_HARNESS_STUDY.md) retains early stops as failed tasks.
[Context summarization](CONTEXT_STUDY.md) reduced main-request input while adding
total tokens and latency. [Tool scheduling](SCHEDULING_STUDY.md) achieved declared
concurrency and beat its shadow wrapper while retaining overhead against tracing.
[Calibration](EMPIRICAL_CALIBRATION.md) preserves unavailable attribution and
unknown operating cost rather than changing runtime coefficients.

These are public, permission-cleared local workloads with retained negative
results, not evidence of universal benefit or production-traffic performance.
Zero paid-provider spend does not make operating cost known. Each study page links
its frozen protocol, checksummed archive and reproduction instructions. Quality,
latency, token use, provider spend and whole-workload operating cost keep their
distinct meanings.

## Validation snapshot

At the initial #195 implementation head, the isolated Windows suite passed
**1,842 tests with 82 skips**; the affected suites passed **238 tests** on both
Python 3.10 and 3.13. PR CI exposed a pre-existing routing fake that invented more
provider time than a fast host request could take. The final test-only correction
adds deterministic zero/sub-millisecond cases; **49 routing/promotion tests** and
all pre-commit checks passed locally. Production study code and archived evidence
were unchanged.

All **17 final PR checks** passed on
`e9a06bf612a56e4e176266e1c08126d1486a94cc`, including Python 3.10/3.13/3.14,
PostgreSQL-backed tests, the pinned LangGraph suite and CLI workflows, wheel smoke,
Docker deployment smoke, standalone Linux/Windows/macOS builds, CodeQL, dependency
review and replay/provenance checks. The [final CI run](https://github.com/dipeshbabu/agentloop/actions/runs/36697149402)
records the platform and optional-dependency coverage. Local skips are not claims
that unavailable integrations were exercised on Windows.

Further ranking, experiments, scale, onboarding and drift work is tracked by
[roadmap #208](https://github.com/dipeshbabu/agentloop/issues/208).
