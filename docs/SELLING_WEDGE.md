# AgentLoop Selling Wedge

## Positioning

AgentLoop is not another LLM observability dashboard. It is the PR-time
performance proof layer for agent workflows.

The claim:

> Trace in. Rewrite plan out. Replay proof in the PR.

## Why This Is Different

The market already has strong tracing, eval, prompt, and cost products. Langfuse
covers observability, prompt management, evals, dashboards, OpenTelemetry, and
framework integrations. Phoenix covers tracing, evals, datasets, experiments,
prompts, and OpenTelemetry/OpenInference. Traeco is close on trace-driven agent
cost intelligence and recommendations.

AgentLoop should avoid selling generic observability. The unique wedge is:

- **Workflow rewrite proof**: not only "this run was expensive," but "this exact
  loop shape should change."
- **PR-native gates**: every agent change can show latency, cost, retry, and
  finding impact before merge.
- **Interop-first ingestion**: use native traces, OpenTelemetry GenAI-style
  spans, OpenAI Agents traces, and framework adapters.
- **Patch-plan bridge**: findings include likely files, source locations,
  affected spans, suggested diff shape, and replay criteria.
- **ROI handoff**: the same trace produces technical diagnosis and buyer-facing
  monthly value.

## Sales Narrative

Lead with one expensive agent workflow, not a platform tour.

1. Buyer sends one trace or generates one from staging.
2. AgentLoop identifies serial tools, repeated context, retry waste, expensive
   low-value steps, and patchable findings.
3. AgentLoop emits a patch plan with likely files and validation gates.
4. AgentLoop compares baseline and candidate traces.
5. AgentLoop posts a PR comment showing whether the change improved production
   economics.
6. AgentLoop turns the measured savings into a pilot value report.

## What To Say

"You already have traces. AgentLoop tells you which agent loop to rewrite and
whether the rewrite actually paid off."

"Observability tells you what happened. AgentLoop turns the trace into a merge
gate for cost, latency, and reliability."

"We do not replace Langfuse, Phoenix, LangSmith, or your OTel stack. We sit above
them as a performance compiler pass."

## What Not To Say

- Do not claim AgentLoop is the only agent observability tool.
- Do not lead with dashboards.
- Do not sell prompt management.
- Do not sell broad autonomous code editing yet.
- Do not build billing before multiple teams accept the optimization proof.

## First Paid Offer

Start with a design-partner optimization audit:

- one agent workflow
- one trace ingestion path
- one diagnosis readout
- one patch plan
- one replay gate recommendation
- one PR-style report
- one ROI estimate

Price the first audits at `$1,000-$5,000`, then convert repeat teams to
`$500-$2,000/month` while the hosted product matures.
