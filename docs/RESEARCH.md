# Using AgentLoop for research

AgentLoop can be used as execution-level instrumentation and evaluation infrastructure for agentic systems. It is useful when the research question depends on what an agent did during a run, how much work it performed, and whether a runtime intervention changed efficiency or reliability.

AgentLoop does not provide mechanistic interpretability, model training, causal identification by itself, benchmark dataset management, statistical significance testing, or human-evaluation assignment. Treat it as the trace and intervention-evidence layer inside a larger research workflow.

## Good research questions

AgentLoop is a good fit for experiments such as:

- sequential versus parallel tool execution;
- ReAct versus planner-executor workflows;
- single-agent versus multi-agent coordination;
- reflection or retry strategies;
- context compression and caching;
- model routing policies;
- loop and tool-oscillation guardrails;
- execution signals associated with success or failure;
- quality, latency, token, retry, and cost tradeoffs across agent designs.

## Experimental unit

One AgentLoop trace should represent one task attempt under one experimental condition. Put experiment identifiers in trace metadata so the raw traces remain self-describing:

```python
from agentloop import trace_agent

with trace_agent(
    "research_agent",
    metadata={
        "experiment_id": "parallel-retrieval-v1",
        "condition": "parallel",
        "dataset": "my-benchmark",
        "task_id": "task-0042",
        "seed": 3,
        "model": "example-model",
        "prompt_version": "v7",
        "git_commit": "abc123",
    },
) as trace:
    run_agent(task)

trace.export_json("runs/parallel/task-0042-seed-3.json")
```

Trace metadata is part of the public trace schema and survives native serialization and supported OpenTelemetry round trips.

## Paired intervention workflow

For an intervention study:

1. State the hypothesis before looking at results.
2. Define a baseline and candidate condition.
3. Keep tasks, models, prompts, tool access, and evaluation rules fixed unless one of them is the intervention.
4. Record the condition and reproducibility metadata on every trace.
5. Run multiple tasks and, when stochasticity matters, multiple seeds or repetitions.
6. Score task quality with domain-appropriate evaluation rather than treating lower cost or latency as success by itself.
7. Compare paired baseline and candidate traces with AgentLoop replay or CI reports.
8. Aggregate effects across tasks outside the single-run report and report uncertainty appropriate to the study design.

Example comparison:

```bash
agentloop replay \
  --baseline runs/baseline/task-0042-seed-3.json \
  --candidate runs/candidate/task-0042-seed-3.json \
  --quality-fixtures evaluation/quality_fixtures.json \
  --min-quality-score 0.9 \
  --json-out runs/comparisons/task-0042-seed-3.json
```

## What to measure

AgentLoop directly reports execution measurements such as:

- end-to-end runtime;
- cumulative instrumented span time;
- model, tool, and retry counts;
- model and tool time;
- input and output tokens;
- repeated-context ratio;
- cost when pricing is complete or provider-reported;
- event status and errors;
- execution graph structure and affected spans.

Quality fixtures can add task-grounded pass/fail or scalar evidence. Built-in scorers support exact matching, containment, bounded glob matching, required fields, and JSON subsets. Trusted local experiments can also use custom Python scorers. The removed `regex` name must be migrated to a bounded text scorer; `json_schema` was only a required-field alias and must be migrated to `required_fields` or `json_subset`.

## Measured versus estimated results

Keep these categories separate in papers and reports.

**Measured evidence** comes from observed traces or task evaluation: runtime, tokens, calls, retries, failures, quality scores, and complete/provider-reported cost. Token counts qualify only when their provenance says they were counted — see [Token measurements](#token-measurements) below.

**Estimated evidence** comes from optimization analysis: proposed latency savings, proposed cost savings, rewrite recommendations, value scenarios, and priority scores.

Optimization cards are useful hypotheses for interventions. They are not experimental results until the proposed change is implemented and measured on a candidate condition.

## Token measurements

Not every token number in a trace is a token count. When a caller supplies text
but no counts, AgentLoop approximates with `len(text.split())`, which is a proxy
for tokens, not a measurement of them. Every model call therefore records a
`token_provenance`, and each trace reports an aggregate `token_status`:

| `token_status` | Suitable to report as a token measurement? |
|---|---|
| `exact` | Yes — every model call counted tokens (provider, tokenizer, or caller-supplied). |
| `empty` | Not applicable — the run made no model calls. |
| `partial` | No — some calls counted tokens and some did not. Report the split, or restrict the analysis to the exact subset. |
| `estimated` | No — word approximations. Usable for relative structure, not for reported token or cost figures. |
| `unavailable` | No — the run recorded no token counts at all. |
| `unspecified` | No — the trace predates provenance, so exactness cannot be established. Re-record it. |

For a published token or cost figure, collect traces whose `token_status` is
`exact`. In practice that means instrumenting through an integration that reads
provider usage (`agentloop.integrations.openai`), importing OTLP spans that carry
`gen_ai.usage.*` attributes, or passing `input_tokens=` / `output_tokens=`
explicitly to `trace_model_call(...)`.

Because a calculated cost is a rate multiplied by a token count, a cost figure is
never more exact than the tokens behind it. `cost_breakdown.token_status` carries
that basis alongside `cost_status`, and replay cost gates go indeterminate when
the token basis is only estimated — see [Cost completeness](#cost-completeness).

## Cost completeness

Do not treat unknown model prices as zero. AgentLoop records `cost_status` as `complete`, `partial`, `unknown`, or `empty`. Cost comparisons and modeled value are only fully evaluable when all relevant calls have known or provider-reported cost **and** the token counts those rates multiply were counted rather than approximated. A replay report reports both inputs separately as `gates.pricing_known` and `gates.token_basis_evaluable`. See `docs/PRICING.md`.

## Reproducibility checklist

For each published experiment, retain at least:

- AgentLoop version;
- trace schema version;
- experiment and condition names;
- dataset and task IDs;
- random seed or repetition index when applicable;
- model/provider identifiers;
- prompt/configuration version;
- source commit;
- evaluation fixture/scorer version;
- raw trace JSON;
- baseline/candidate comparison output;
- aggregation code used for paper-level statistics.

Avoid publishing raw prompts, tool arguments, outputs, URLs, credentials, or customer data unless the dataset and release process explicitly permit it. Synthetic or redacted traces are safer for public artifacts.

## Statistics and claims

A single baseline/candidate pair is useful for debugging, not for a general research claim. For comparative studies, use enough independent tasks or repetitions to characterize variability. AgentLoop intentionally does not choose the statistical test for you because the correct estimator depends on the experiment design.

When reporting results, distinguish:

- per-run measurements from aggregated results;
- paired from unpaired comparisons;
- preregistered metrics from exploratory findings;
- exact measurements from optimizer estimates;
- successful runs from censored, timed-out, or invalid runs.

## Interoperability

Researchers do not have to adopt AgentLoop as the only tracing stack. Native traces can be exported to OpenTelemetry, and supported OpenTelemetry GenAI-style traces can be imported for diagnosis and replay. This makes AgentLoop suitable as an analysis layer over an existing experiment harness.

## Minimal offline example

Run:

```bash
python examples/research_experiment_demo.py
```

The example creates deterministic synthetic baseline and candidate traces with experiment metadata, exports both traces, and writes a replay comparison without a network call or paid model API.
