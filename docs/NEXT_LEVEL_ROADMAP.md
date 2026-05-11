# AgentLoop Next-Level Roadmap

Date: 2026-05-11

## Research readout

The market is already crowded around AI observability, evals, prompts, and dashboards.
The durable opening for AgentLoop is narrower and sharper:

> AgentLoop should become the performance compiler for AI agent workflows: ingest traces,
> prove waste, generate a rewrite, validate the rewrite, and show the ROI.

Do not position AgentLoop as another trace viewer. Position it as the thing teams use after
they already have traces and want production agent runs to become cheaper, faster, and safer.

## Evidence

- OpenTelemetry now has GenAI semantic conventions in development, including model spans,
  agent spans, events, metrics, and provider-specific conventions. AgentLoop should ingest
  and emit this shape instead of inventing a proprietary trace format.
  Source: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- OpenTelemetry agent spans cover agent invocation, workflow spans, and tool execution.
  This gives AgentLoop a standard graph substrate for optimizer logic.
  Source: https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
- Langfuse already covers tracing, prompt management, evals, sessions, user tracking,
  agent graphs, cost, latency, Python/JS SDKs, many integrations, and OpenTelemetry.
  Source: https://langfuse.com/docs/
- Arize Phoenix already covers OpenTelemetry/OpenInference tracing, evals, prompt iteration,
  datasets, experiments, and auto-instrumentation across major frameworks and providers.
  Source: https://arize.com/docs/phoenix
- Braintrust already turns production traces into eval datasets and supports trace inspection,
  evals, monitoring, prompts, datasets, and IDE workflows.
  Source: https://www.braintrust.dev/
- Opik already claims observability, evals, prompt engineering, and automated agent optimization
  algorithms. Its messaging moves beyond trace viewing.
  Source: https://www.comet.com/docs/opik/
- Helicone AI Gateway already combines provider routing, fallback, and observability across
  many model providers.
  Source: https://docs.helicone.ai/gateway/overview
- OpenAI Agents SDK has built-in tracing and custom processors, which gives AgentLoop a high-value
  integration path without wrapping every SDK call manually.
  Source: https://openai.github.io/openai-agents-python/tracing/
- Traeco is very close to AgentLoop's current cost-intelligence wedge: agent traces, concrete
  cost cuts, SDK integration, pricing around trace volume, and recommendations.
  Source: https://traeco.dev/
- Rulio is close to the trace-analysis-to-coding-agent loop: scan all traces, produce stable
  findings, hand them to coding agents, re-scan, and compare regressions.
  Source: https://rulio.ai/

Conclusion: claiming that nobody has done agent observability or cost optimization would be weak.
The stronger claim is an execution claim: AgentLoop produces verified workflow rewrites, not just
recommendations.

## Product thesis

AgentLoop should own "agent loop performance engineering."

The core user promise:

> Send AgentLoop traces from LangGraph, CrewAI, OpenAI Agents SDK, Vercel AI SDK, or OpenTelemetry.
> AgentLoop returns a ranked set of verified workflow rewrites with code patches, eval gates,
> expected savings, and proof after replay.

## The moat

Build a closed optimization loop:

1. Ingest: accept AgentLoop native traces, OpenTelemetry GenAI spans, OpenInference-style spans,
   and OpenAI Agents SDK traces.
2. Diagnose: detect repeated context, serial independent tools, retry loops, tool oscillation,
   context growth, expensive low-value model steps, and runaway agents.
3. Rewrite: generate framework-specific patches for LangGraph, CrewAI, OpenAI Agents SDK,
   Vercel AI SDK, and plain Python/TypeScript.
4. Validate: replay before/after runs on production-derived fixtures and run quality gates.
5. Prove: compare cost, latency, retries, quality score, and reliability risk after the patch.

The differentiator is not "AI says optimize this." The differentiator is:

> AgentLoop found the waste, changed the workflow, replayed the trace, and proved the improvement.

## Build order

### 1. Trace Interop Layer

Ship import/export for OpenTelemetry GenAI spans and OpenAI Agents SDK processors.

Minimum features:

- OTLP JSON import into `AgentEvent`.
- AgentLoop trace export to OTel GenAI-like JSON.
- OpenAI Agents SDK tracing processor that sends spans to AgentLoop.
- Span fields for `gen_ai.system`, `gen_ai.operation.name`, model, token usage,
  agent name, tool name, conversation ID, and parent span.

Why first:

AgentLoop cannot win by asking teams to replace their tracing stack. It should sit above existing
trace sources and become the optimizer.

### 2. Optimization Evidence Model

Upgrade optimization cards from human hints into machine-actionable findings.

Each finding should include:

- stable finding ID
- severity
- affected spans
- evidence rows
- savings formula
- confidence reason
- framework detected
- recommended rewrite type
- validation plan
- before/after acceptance criteria

This makes cards usable by dashboards, CI, coding agents, PR comments, and API clients.

### 3. Patch Generator

Add a `agentloop patch` command that generates a patch plan first, then optionally writes a branch.

Initial patch types:

- parallelize independent tool calls
- cache stable prompt prefix
- add structured-output repair path
- route cheap classification/extraction/summarization steps to a smaller model
- add runaway-loop guardrail
- batch repeated summarization or extraction calls

First targets:

- plain Python async functions
- LangGraph node functions
- OpenAI Agents SDK workflows

Do not start with broad autonomous code editing. Start with constrained templates tied to specific
evidence types.

### 4. Replay And Regression Gate

Add a `agentloop replay` command that runs before/after fixtures and emits a comparison report.

Gate on:

- cost per run
- latency per run
- retry count
- tool-call count
- output schema validity
- optional user-provided quality scorer

This is where AgentLoop stops being a dashboard and becomes a performance engineering system.

### 5. CI And PR Workflow

Add:

- GitHub Action that comments on PRs with predicted cost/latency impact.
- `agentloop ci --budget-cost-delta 10% --budget-latency-delta 20%`.
- Markdown and JSON reports designed for pull request comments.

This turns AgentLoop into a buying trigger: every agent PR can show whether it made production
economics better or worse.

## Product surface

CLI:

- `agentloop import-otel`
- `agentloop export-otel`
- `agentloop diagnose`
- `agentloop patch --dry-run`
- `agentloop replay`
- `agentloop ci`

Hosted:

- project trace ingestion
- optimization queue
- finding clusters
- patch proposals
- before/after replay reports
- ROI and reliability dashboards
- PR comments and Slack alerts

SDK:

- native trace decorators remain
- OpenAI Agents SDK processor
- OTLP receiver/client
- LangGraph, CrewAI, Vercel AI SDK adapters

## Positioning

Bad positioning:

- "AI observability for agents"
- "LLM tracing and evaluation"
- "Dashboards for cost and latency"

Better positioning:

- "The agent workflow optimizer"
- "Find and fix agent-loop waste before it hits production"
- "Trace in. Patch out. ROI proven."
- "A profiler and compiler pass for production AI agents"

## 30-day execution plan

Week 1:

- Add OTel GenAI import/export.
- Define the finding schema.
- Convert existing optimization cards to evidence-backed findings.

Week 2:

- Build OpenAI Agents SDK trace processor.
- Add `agentloop diagnose` with JSON and Markdown output.
- Add tests using synthetic OTel/OpenAI span fixtures.

Week 3:

- Build dry-run patch plans for three high-confidence rewrites:
  parallelize tools, cache context, structured-output repair.
- Add framework detection for plain Python and LangGraph examples.

Week 4:

- Build replay comparison reports.
- Add GitHub Action proof-of-concept.
- Create one killer demo: a slow/costly LangGraph or OpenAI Agents workflow, AgentLoop patch plan,
  replay proof, and PR comment.

## What not to build next

- Do not build another generic trace timeline before interop and diagnosis are stronger.
- Do not build prompt management yet; Langfuse, Phoenix, Braintrust, and Opik already own that.
- Do not build broad billing before the optimizer produces repeatable proof.
- Do not claim uniqueness on observability or cost dashboards; the internet already has several.
- Do not chase every framework at once. Win one workflow deeply, then generalize.

## One-sentence next move

Build `agentloop diagnose` on top of OpenTelemetry/OpenAI trace ingestion, then make it produce
machine-actionable findings that can become patch plans and replay-verified PR comments.
