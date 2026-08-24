# First useful result

AgentLoop has two orchestration commands for people who do not yet need the lower-level workflow.

## 1. Verify the install without an account or API key

```bash
agentloop quickstart
```

`quickstart` creates a deterministic synthetic trace locally, analyzes it, and prints several concrete findings. It does not use a network connection, model provider, account, API key, database, or paid API.

The trace is written to:

```text
runs/agentloop_quickstart.json
```

You can choose another path:

```bash
agentloop quickstart --out /tmp/agentloop-example.json
```

For the combined machine-readable report:

```bash
agentloop quickstart --json-out runs/quickstart_analysis.json
```

The trace is marked with:

```json
{
  "synthetic": true,
  "source": "agentloop_quickstart"
}
```

so it cannot be confused with production evidence.

## 2. Analyze one real trace

After instrumenting an agent and exporting a trace:

```bash
agentloop analyze runs/my_agent.json
```

This one command combines the existing report, diagnosis, and optimization analysis and prints the most useful findings first.

To retain the complete structured output:

```bash
agentloop analyze runs/my_agent.json --json-out runs/my_agent_analysis.json
```

The JSON contains four sections:

- `trace`: the validated native trace;
- `report`: measured runtime, token, call, retry, and cost information;
- `diagnosis`: evidence-backed findings;
- `optimization`: rewrite opportunities and savings-selection metadata.

## 3. Move to proof

`analyze` is for finding a problem. Once you change the agent, compare the before and after runs:

```bash
agentloop replay \
  --baseline runs/baseline.json \
  --candidate runs/candidate.json
```

Add quality fixtures when faster or cheaper is not sufficient evidence by itself:

```bash
agentloop replay \
  --baseline runs/baseline.json \
  --candidate runs/candidate.json \
  --quality-fixtures evaluation/fixtures.json \
  --min-quality-score 0.9
```

For pull-request gating, use the same inputs with `agentloop ci`.

## Advanced commands remain available

`quickstart` and `analyze` orchestrate existing AgentLoop capabilities. They do not replace the lower-level commands. Use `report`, `diagnose`, `optimize`, `patch`, `replay`, `quality-report`, `ci`, store commands, and API commands when you need control over one stage of the workflow.
