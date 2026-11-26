# AgentLoop roadmap

AgentLoop connects recorded execution, optimization findings, applied
interventions, and measured outcomes. Core analysis stays local and framework
neutral. This roadmap describes work on `main`; it does not announce a release
or change the published package version. See [Unreleased changes](../CHANGELOG.md#unreleased).

## Completed evidence workflow

The [evidence roadmap](https://github.com/dipeshbabu/agentloop/issues/145)
is implemented on `main`:

| Area | Available behavior |
| --- | --- |
| Measurement correctness | Token provenance, qualified parallelization evidence, validated storage pagination, read-only diagnosis GETs, precise scorer names, and explicit trace ownership |
| Canonical findings | One versioned finding registry used by reports, plans, diagnosis, dashboard, and CI, with explicit incomplete-analysis diagnostics |
| Estimator provenance | Versioned formulas, coefficients, assumptions, input snapshots, and uncalibrated labels retained in persisted findings |
| Operation semantics | Framework-neutral operation kinds alongside legacy event categories |
| Intervention evidence | Immutable project-scoped records linking original findings to baseline/candidate outcomes and quality/gate results |
| Repeated studies | Deterministic pairing, explicit unmatched cases, condition/paired statistics, cost completeness, and optional seeded intervals |
| Interoperability | Pinned GenAI/OpenInference/MCP fixtures, preserved telemetry evidence, and a shared integration conformance harness |
| Sharing and compatibility | Single-file offline HTML reports and an API v1 namespace with documented pre-1.0 aliases |
| CI evidence | Explicit separation of synthetic repository self-tests from supplied application trace comparisons |

Run the [complete artifact workflow](EVIDENCE_WORKFLOW.md) to see how the outputs
answer the roadmap's nine evidence questions. The example includes successful,
failed-quality, and unknown-cost cases. Its fixture outcomes demonstrate the
contracts and are not empirical performance claims.

## Completed optimization evidence roadmap

The [#208 implementation and evidence review](OPTIMIZATION_EVIDENCE_REVIEW.md)
maps all eleven completed workstreams to merged changes and validation artifacts:
finding trust/ranking, candidate experiments, context/reuse and structural
families, retention/aggregation, onboarding, drift and cross-workload studies.
The [cross-workload benchmark](USEFULNESS_BENCHMARK.md) and
[non-agent CPU study](NON_AGENT_STUDY.md) preserve false positives, quality losses,
unknown costs and attribution limits alongside successful cases. These APIs are
included in 0.1.0; research examples require a source checkout. Their availability
does not authorize deployment.

## Completed quality-constrained optimization roadmap

[#394](https://github.com/dipeshbabu/agentloop/issues/394) proposed turning
AgentLoop from a profiler and evidence tool into a quality-constrained
optimization toolkit. Every workstream is merged:

| Issue | Delivered | Guide |
| --- | --- | --- |
| #395 Source provenance | Opt-in source receipts; patch plans target exact definitions | [Source provenance](SOURCE_PROVENANCE.md) |
| #396 Execution IR | `agentloop.ir/v1` with declared, observed, inferred and unknown evidence | [Execution IR](EXECUTION_IR.md) |
| #397 Timing sensitivity | Simulated critical-path gains, and checks against measured runs | [Timing sensitivity](TIMING_SENSITIVITY.md) |
| #398 Transformation registry | Dry-run passes, and opt-in validation in a git worktree | [Transformation registry](TRANSFORMATIONS.md) |
| #399 Request-local reuse | Declared-safe, request-scoped reuse and protected context deduplication | [Request-local reuse](REQUEST_REUSE.md) |
| #400 Scheduling | Declared-independent tool calls compiled for the scheduler | [Compiling a schedule](SCHEDULE_COMPILER.md) |
| #401 Retry and loop passes | Retry causes, shadow-only bounded limits | [Retry and loop passes](RETRY_PASSES.md) |
| #402 Optimization search | Seeded, budgeted search with a held-out decision for review | [Optimization search](OPTIMIZATION_SEARCH.md) |
| #403 Calibration | Stratified correction, held-out evaluation, review before use | [Conditioned calibration](CONDITIONED_CALIBRATION.md) |
| #404 Coding agents | A local MCP server and an agent skill | [Coding agents (MCP)](MCP.md) |
| #405 Telemetry | Optional process, subprocess and inference measurements | [System telemetry](SYSTEM_TELEMETRY.md) |
| #406 Benchmark and pilot | Independently labelled benchmark, negative control, and one AI-run first-use pilot | [Optimization benchmark](OPTIMIZATION_BENCHMARK.md) |
| #407 Modularization | Store, CLI, dashboard and improvement workflow split behind facades | Contract tests in `tests/` |
| #408 Evidence docs | Archived and current benchmark results separated | [Finding benchmark](FINDING_BENCHMARKS.md) |

Against the roadmap's release exit criteria:

- **Compatibility.** Each split kept its import path, with a public-surface
  contract test; the CLI and dashboard are compared against snapshots taken
  before the change. New trace fields are optional metadata, so native and
  TypeScript traces still interoperate.
- **An executable candidate with an independently measured outcome.**
  `execute_candidate` validates a change in a temporary worktree and leaves the
  checkout untouched. The benchmark and the pilot both measured accepted and
  rejected outcomes against independent labels.
- **Transforms declare their contract** (preconditions, evidence, tolerated
  effects, validation and rollback) and abstain when evidence is missing.
  Inferred evidence never authorizes execution.
- **Held-out checks and failures stay visible.** Every search attempt,
  rejected candidate and unknown cost is kept, and tuning and held-out cases
  are checked for leakage.
- **Reported:** optimizer overhead, recommendation precision, intervention
  success, prediction error, time to first useful finding and negative controls,
  in the [optimization benchmark](OPTIMIZATION_BENCHMARK.md). It also reports
  the uncomfortable results: the top-ranked finding cut quality, the real
  waste went undetected, and runtime noise was large at that scale.
- **No deployment, arbitrary edits or promotion without approval.** Passes
  are dry runs, execution is opt-in, loop limits are shadow-only and calibration
  needs a recorded review.

Still open: evidence of independent human developer adoption. The one pilot
so far was run by an AI coding agent.

## Future work to scope separately

- Validate the guided first-use path with developers connecting existing agents,
  then freeze held-out correctness checks from their actual workloads. See
  [guided validation](GUIDED_VALIDATION.md) and [adoption gaps](ADOPTION_GAPS.md).
- Extend the existing offline calibration with larger independent task sets and
  attributable interventions before adopting fitted runtime coefficients.
- Track changing telemetry conventions and expand SDK/framework conformance
  coverage with pinned, reproducible inputs.
- Consider splitting storage behind a compatibility facade and organizing
  CLI/dashboard features without changing their contracts.
- Refine impact scenarios and the implemented retention, aggregation and drift
  contracts based on new concrete workload evidence.
- Add constrained rewrite guidance where recorded evidence and task-specific
  quality gates can validate it.

These directions need focused issues and compatibility decisions before
implementation. They are not implicit additions to the completed evidence backlog.

## Boundaries

AgentLoop does not replace dataset hosting, annotation assignment, prompt
management, model-training tools, general observability, or experiment-specific
statistical analysis. It does not require autonomous source-code editing or a
hosted service. Heuristic optimizer estimates remain hypotheses until an applied
change is measured, and a passing fixture does not establish real-world value.

AgentLoop is not a runtime enforcement layer or a sandbox. Its harness
policies are a cooperation contract with code that chooses to call them, and
candidate validation runs host-supplied commands in a git worktree, not in an
isolated environment. Use your platform's sandboxing for untrusted code. It is
not an agent framework either: it records, analyzes and proposes changes to
agents built with whatever framework you already use, and never runs your agent
for you outside commands you supply.

## Proposing work

Use the feature-request form with a concrete workflow problem, a synthetic trace
or reproduction, expected evidence, compatibility effects, and validation plan.
Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for scope, checks, and review.
