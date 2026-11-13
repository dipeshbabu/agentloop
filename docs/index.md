---
hide:
  - navigation
  - toc
---

<div class="al-intro" markdown>
<div markdown>

# AgentLoop

<p class="al-lead">Record agent execution, investigate wasted work, and verify an improvement with your own quality checks.</p>

Trace model and tool calls, review the evidence behind a finding, and compare
baseline and candidate executions on the same tasks.

[Install AgentLoop](installation.md){ .md-button .md-button--primary }
[Guided optimization](GUIDED_OPTIMIZATION.md){ .md-button }

</div>
<div class="al-example" markdown>

## Get a useful trace

```bash
python -m pip install agentloop-profiler
agentloop quickstart
agentloop analyze runs/agentloop_quickstart.json \
  --html runs/report.html
```

Open `runs/report.html` to inspect the timeline and findings.
This example runs locally with synthetic data.

</div>
</div>

<div class="al-start" markdown>

## Start with your agent

Guided optimization creates a Python runner and task examples, captures your
baseline, and freezes its findings before you apply a change. The comparison
checks both answers against independently declared criteria and reports each
quality and performance gate.

```bash
agentloop improve init --agent my_agent:answer
agentloop improve capture --out baseline -- python agentloop_runner.py
agentloop improve start --baseline baseline/capture.json --out optimization
```

Read `optimization/plan.md`, implement one change, then capture the candidate.
The [complete guide](GUIDED_OPTIMIZATION.md) includes a runnable offline example,
rejection cases, and the before-and-after report. Guided commands require
AgentLoop 0.2.0 or the [current source checkout](installation.md#current-source).

</div>

## Find your next step

<div class="grid cards" markdown>

- **[Connect your framework](INTEGRATIONS.md)**

    Instrument Python agents or bring traces from Hermes, LangGraph, Harbor,
    Omnigent and OpenTelemetry.

- **[Understand a finding](FINDING_RULES.md)**

    Inspect supporting spans, assumptions, risk and estimator provenance before
    choosing a change.

- **[Verify correctness](STRUCTURED_QUALITY.md)**

    Define task-specific checks and inspect rejected candidates, missing usage
    and incomplete captures.

- **[Compare runs in CI](CI.md)**

    Evaluate supplied application traces and retain the reports and gate decisions.

- **[Read the case studies](GUIDED_VALIDATION.md)**

    Follow a successful Hermes example and a rejected local-model experiment,
    with retained measurements and limitations.

- **[Use the SDK and API](python-sdk.md)**

    Record traces in Python or TypeScript and work with the HTTP API.

</div>

<div class="al-start" markdown>

## Read the results in context

Findings distinguish observed execution from inferred assumptions and estimated
savings. Candidate comparisons retain measured outcomes, independent quality
results and missing evidence. Unknown costs stay unavailable.

The [adoption and measurement report](ADOPTION_GAPS.md) describes the remaining
work on external developer onboarding, representative workloads and broader
performance evidence.

</div>
