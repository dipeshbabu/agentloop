# AgentLoop Product Plan

## Wedge

AgentLoop starts as a profiler for AI agent workflows. The wedge is not generic observability. The wedge is measurable agent-loop waste: slow tools, repeated context, unnecessary retries, high-token steps, and sequential calls that should be parallel.

## First buyer

The first buyer is a small team running agent workflows in production or close to production:

- research agents
- coding agents
- browser agents
- internal workflow automation agents
- customer support agents with tools

These teams already feel cost and latency pain, but they do not yet have agent-specific performance tooling.

## v0.1 promise

"Send us one trace. We will show where your agent wastes time and money."

## v0.2 promise

"Install the SDK and get an automatic performance audit for every agent run."

## v0.3 promise

"AgentLoop suggests exact workflow rewrites: parallelize these calls, cache this prefix, split this model step, repair this retry path."

## Sellable demo

A baseline research agent takes longer and spends more tokens. The optimized agent caches stable context, batches summarization, removes retries, and parallelizes read steps. The dashboard shows latency, cost, repeated context, retries, and recommendations.

## Pricing direction

Start with design partners. Then charge by traced runs or seats:

- Free: local SDK and 1 project
- Pro: hosted dashboard, team projects, trace history
- Enterprise: private deployment, eval gates, SSO, custom integrations

## Next integrations

1. LangGraph
2. OpenAI Agents SDK
3. AutoGen / CrewAI
4. Browser-use style agents
5. Coding-agent traces
