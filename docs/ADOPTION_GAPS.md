# Remaining product and measurement gaps

AgentLoop can now guide a developer from initialization through a manually
applied change and independently checked before/after evidence. The release
work prioritizes this path over adding another control or experiment system.

| Gap | Current evidence | Next useful validation |
| --- | --- | --- |
| First use by someone unfamiliar with the project | Generated offline onboarding and installed-wheel tests | Observe consenting developers connecting existing agents; record time to first useful finding, failed steps and required help |
| Representative agent work | Small Hermes repository questions, older model/tool studies, and a reproducible three-workload guided suite | Collect coding, retrieval and tool-heavy task sets from actual applications, with owners' independent acceptance criteria |
| Recommendation precision | Native evidence/risk rankings, explicit assumptions, retained false positives and rejected changes | Review recommendations against independent labels; report unsupported recommendations and abstentions alongside accepted ones |
| Quality coverage | Frozen task inputs and declared per-task checks; a wrong fast candidate cannot pass those checks | Expand adversarial and held-out tasks; measure quality dimensions beyond exact structured answers |
| Causal performance | Per-attempt measurements and disclosed fixed-order confounds | Counterbalance conditions, control cache/load effects, and increase independent task/host/model coverage |
| Instrumentation overhead | Counterbalanced CPU controls separate recording, JSON export and native analysis at declared span volumes; model controls retain quality failures | Repeat controls around actual application execution, across hosts and realistic payload sizes |
| Cost | Actual token provenance and qualified model-price estimates | Obtain authorized billing data; retain unknown local hardware/energy cost instead of inventing dollars |
| Hermes compatibility | 0.21.6 contract coverage and real 0.22.7/0.23.1 conformance | Revalidate future hook changes against the pinned runtime scenarios before extending the supported range |
| TypeScript adoption | npm package `agentloop-profiler`, installed Node API interoperability, esbuild/Chromium package checks and a tag-driven npm release workflow | Observe integration into developer-owned applications and extend the browser/bundler target matrix as needed |

No external users, production deployments or customer savings are established by
these tests and studies. PyPI download counts do not identify unique developers
or deployments. The retained small experiments support their specific observed
outcomes, including failures, without establishing a general optimization rate.

For the next iteration, prioritize the first-use observation and one real
application's held-out correctness suite. Those will show whether developers can
find a useful inefficiency and prove a safe improvement with less assistance.

## First-use developer pilot

Use a consenting developer's existing agent and a fresh installed-wheel
environment. Have the application owner freeze a small pilot task set and
independent acceptance checks before measuring a candidate. Keep held-out tasks
separate until the proposed change and quality criteria are fixed.

Record the start time, package/source version, time of first valid trace, time
of first actionable finding, failed steps, assistance supplied, and the time the
developer reaches an accepted or rejected report. Keep the task manifest,
baseline and candidate captures, frozen guided session, linked intervention,
paired study and every failed attempt. Record manual effort with the existing
study evidence. An optimization rejected by the quality gates is a completed
observation with a negative result.

The maintainer must arrange the participant's consent and permission to retain
their application data. A sanitized private evidence bundle can use the existing
trace/study artifacts. No external participant observations are currently recorded.

## Reproduce scoped overhead measurements

From the source checkout, replace `YOUR_COMMIT` with its full commit hash.

```bash
uv run --frozen python -m scripts.measure_instrumentation --source-revision YOUR_COMMIT --out runs/instrumentation-controls
```

The protocol records source hashes, interpreter, host, planned samples and order
before execution. Default sizes are 32, 256 and 1,024 tool spans, with six paired
repetitions per size. Traced-first and untraced-first order alternate. Each pair
checks the same deterministic arithmetic result, validates a native trace and
retains recording, export and analysis timings. Export measures JSON serialization
and a buffered local file write. Failed and cancelled attempts retain their
records. The shared `summarize_values` helper preserves unavailable samples in
the planned denominator. A fresh output directory protects earlier results.

These CPU controls quantify local operations on their declared fixtures. Real
agent inference, external transport, filesystem durability and operating cost
need their own controls. Public finding fixtures and owned CPU examples provide
regression evidence. Independent developer adoption remains unmeasured.

The [retained evidence index](../research/instrumentation-2026-11/index.json)
contains 118 checksummed files, including both control runs and the existing
and corrected finding evaluations. Both control runs completed all 18 planned
pairs. Their raw timings, traces and native analyses remain available.

```bash
uv run --frozen python -m examples.real_agent_study.archive research/instrumentation-2026-11/index.json --out runs/retained-instrumentation
```

The final shared-host run's median recording differences were about 1.75 ms for
32 spans, 10.07 ms for 256 spans and 32.96 ms for 1,024 spans. The first run and
all individual samples are retained because host load and filesystem latency
varied. These values describe this fixture and machine. The same archive retains
the schema-repair false finding before its correction and the complete evaluation
afterward. The existing initial regression baseline and its labels remain intact.
