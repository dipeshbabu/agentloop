# AgentLoop roadmap

AgentLoop's goal is to make AI agent workflows measurable and optimizable: ingest
an execution trace, identify waste, propose a focused workflow change, and prove
the result against performance and quality gates.

This roadmap communicates project direction rather than promising release dates.
Priorities may change as maintainers learn from issues, integrations, and real
workloads. Open an issue before starting a substantial roadmap item so its scope
and compatibility impact can be agreed on first.

## Current foundation

The repository already provides:

- native tracing for model, tool, retry, and workflow events;
- adapters for OpenAI, OpenAI Agents, LangGraph, CrewAI, Vercel AI SDK events,
  and OpenTelemetry-style traces;
- evidence-backed diagnosis and optimization findings;
- dry-run patch plans tied to replay acceptance criteria;
- before-and-after replay, schema, quality, cost, latency, and retry gates;
- CLI, local dashboard, SQLite, Postgres, and HTTP API surfaces; and
- pull-request reports and GitHub Actions performance gates.

## Near-term priorities

### Trace interoperability

- Track stable OpenTelemetry GenAI conventions as they evolve.
- Preserve unknown fields during import and export where practical.
- Add conformance fixtures for supported framework and SDK versions.
- Improve diagnostics for incomplete, malformed, or unsupported traces.

### Optimization evidence

- Improve confidence explanations and savings estimates.
- Add deterministic findings for context growth, runaway loops, and tool
  oscillation.
- Make recommendation ordering stable across storage backends.
- Expand regression fixtures for boundary values and ambiguous execution graphs.

### Research and reproducibility

- Keep experiment metadata portable in the public trace schema.
- Add task-focused examples for paired interventions and agent-architecture comparisons.
- Separate measured trace evidence from optimizer estimates in research-facing reports.
- Add optional batch aggregation helpers without choosing a statistical test on behalf of the study.
- Publish reproducibility guidance for task IDs, conditions, seeds, model/config versions, source commits, and evaluation fixtures.

### Safe rewrite assistance

- Keep generated patch plans reviewable and evidence-linked.
- Expand constrained rewrite templates only when replay gates can validate them.
- Add framework-specific guidance without making core analysis depend on those
  frameworks.
- Avoid automatic source modification where confidence or quality risk is too
  high.

### Reliability and self-hosting

- Strengthen Postgres parity, migrations, backup guidance, and retention tools.
- Add deployment observability and clearer readiness diagnostics.
- Continue hardening project isolation and API authorization boundaries.
- Publish reproducible container and upgrade procedures.

### Contributor experience

- Grow small, well-scoped issues suitable for first-time contributors.
- Add architecture decision records for consequential compatibility choices.
- Improve API examples and task-focused documentation.
- Keep setup and checks reproducible through uv and pre-commit.

## Non-goals

AgentLoop does not currently aim to replace a general-purpose observability,
prompt-management, or evaluation platform. Core tracing and analysis must remain
usable locally without a hosted service or live model provider. Broad autonomous
code editing is also out of scope until changes can be constrained, reviewed,
and verified reliably.

AgentLoop also does not aim to replace model-training frameworks, mechanistic
interpretability tooling, benchmark dataset management, or experiment-specific
statistical analysis. Research workflows can use AgentLoop as the execution and
intervention-evidence layer while keeping those responsibilities in dedicated
tools.

## Proposing roadmap work

Use the feature-request issue form and include:

- the workflow or compatibility problem;
- a representative synthetic trace or minimal reproduction;
- the proposed public API or data-model impact;
- expected performance or quality evidence; and
- a testing and migration approach.

Roadmap work follows the same review, compatibility, and changelog requirements
as other contributions. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full
process.
