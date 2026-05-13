# AgentLoop Pilot Readiness

This checklist is the fastest path from local product to paid design partner.
The goal is not a broad SaaS launch. The goal is a repeatable paid pilot where a
buyer can send one agent trace and get a credible waste report, rewrite plan,
replay comparison, and ROI estimate.

## Pilot Promise

Send AgentLoop one production or staging agent trace. AgentLoop will show:

- where the agent wastes latency, tokens, retries, and tool calls
- which workflow rewrites are likely to reduce cost or runtime
- the replay gates needed to prove the change did not regress the workflow
- a buyer-facing value report for the pilot follow-up

## Target Buyer

Prioritize teams already running multi-step agents in production or late-stage
staging:

- LangGraph research or workflow agents
- OpenAI Agents SDK workflows
- CrewAI or custom Python agents
- internal support, research, coding, browser, or operations agents

Avoid teams that only have single-turn chat apps. AgentLoop is strongest when
the workflow loops, calls tools, retries, branches, or carries repeated context.

## Local Pilot Gate

Run these before every buyer demo:

```bash
pip install -e ".[all,dev]"
python -m pytest -q
agentloop demo-all
agentloop diagnose --out runs/diagnosis.md --json-out runs/diagnosis.json
agentloop patch --dry-run --out runs/patch_plan.md --json-out runs/patch_plan.json
agentloop replay --out runs/replay_report.md --json-out runs/replay_report.json
agentloop value-report --out runs/value_report.json --runs-per-month 5000
```

The local gate is ready when tests pass and these artifacts exist:

- `runs/diagnosis.md`
- `runs/patch_plan.md`
- `runs/replay_report.md`
- `runs/value_report.json`

Run the production configuration check after setting production-like environment
variables. `--no-check-api --no-check-store` skips live network and database
calls, but it still validates that the deployment configuration is safe:

```bash
agentloop production-check --no-check-api --no-check-store
```

## Hosted Pilot Gate

Use Postgres, API-key auth, and explicit CORS for any internet-facing pilot.

```bash
copy .env.example .env
# Edit .env and replace every placeholder secret.
docker compose up --build
```

Create a project key and upload the demo trace:

```bash
agentloop remote-create-api-key acme pilot --api-url http://localhost:8000 --admin-api-key YOUR_ADMIN_KEY
agentloop upload runs/research_agent_baseline.json --api-url http://localhost:8000 --api-key al_xxx
agentloop remote-usage --api-url http://localhost:8000 --api-key al_xxx
```

Run the smoke check:

```bash
AGENTLOOP_API_URL=http://localhost:8000 AGENTLOOP_ADMIN_API_KEY=YOUR_ADMIN_KEY python scripts/smoke_api.py
```

For a public deployment, `AGENTLOOP_API_URL` must be HTTPS and
`AGENTLOOP_CORS_ORIGINS` must be the exact dashboard origin.

## Demo Flow

1. Show the buyer the baseline trace report.
2. Open the diagnosis and point to one high-confidence finding.
3. Open the patch plan and explain the proposed workflow rewrite.
4. Open the replay report and explain the pass/fail gates.
5. Open the value report and convert savings into monthly ROI.
6. Ask for one real trace from their highest-volume or slowest agent workflow.

## Paid Pilot Offer

Start with a manual design-partner package:

- one project
- trace ingest by SDK, CLI, or uploaded JSON
- one optimization readout per week
- value report for each candidate fix
- replay gate recommendation for production rollout

Suggested starting price:

- one-time optimization audit: `$1,000-$5,000`
- recurring design partner pilot: `$500-$2,000/month`

Do not build billing, SSO, team management, or broad account administration
until a buyer asks for them after seeing the optimization proof.

## Go/No-Go

Ready to sell a pilot:

- full test suite passes
- local pilot artifacts generate from `agentloop demo-all`
- hosted API has API-key auth and Postgres
- one buyer can upload a trace and receive optimization, replay, and value reports
- the sales call is focused on cost, latency, reliability, and proof

Not ready for broader launch:

- no real customer trace has been analyzed yet
- replay gates have not been accepted by a buyer
- the dashboard is the main pitch instead of the optimization proof
- onboarding requires live debugging from the founder every time
