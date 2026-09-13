# AgentLoop roadmap

AgentLoop connects recorded execution, optimization findings, applied
interventions, and measured outcomes. Core analysis stays local and framework
neutral. This roadmap describes work on `main`; it does not announce a release
or change the published package version. See [Unreleased changes](../CHANGELOG.md#unreleased).

## Completed evidence workflow

The [0.7 evidence roadmap](https://github.com/dipeshbabu/agentloop/issues/145)
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

## Future work to scope separately

- Calibrate estimator families using accumulated real intervention evidence and
  suitable research designs.
- Track changing telemetry conventions and expand SDK/framework conformance
  coverage with pinned, reproducible inputs.
- Consider splitting storage behind a compatibility facade and organizing
  CLI/dashboard features without changing their contracts.
- Refine impact scenarios, retention, and operational tooling based on concrete
  workload requirements.
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

## Proposing work

Use the feature-request form with a concrete workflow problem, a synthetic trace
or reproduction, expected evidence, compatibility effects, and validation plan.
Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for scope, checks, and review.
