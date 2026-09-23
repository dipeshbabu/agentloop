# Same-agent harness ablations

`agentloop study ablation` summarizes a frozen host-run experiment. It does not
execute agents, reset tools, score outputs, or turn stored traces into new runs.
The offline protocol and synthetic example are available; the permission-cleared
real-agent study required by [#188](https://github.com/dipeshbabu/agentloop/issues/188)
still requires workload repositories, task data, owner-approved scorers, and
provider budgets. No empirical harness-benefit claim is made here.

## Run the offline example

```bash
uv run python examples/harness_ablation.py --out runs/harness-ablation
uv run agentloop study ablation runs/harness-ablation/bundle.json --out runs/harness-ablation/report.md --json-out runs/harness-ablation/report.json
```

The [example](../examples/harness_ablation.py) constructs synthetic observations,
native traces, an existing [study manifest](STUDIES.md), an immutable
[intervention record](INTERVENTIONS.md), and JSON/Markdown reports. Its numbers
are fixtures, not measured timings, scorer evaluations, provider charges, or
evidence that the fixture guard implements a recommended rewrite. The ledger
link tests artifact preservation only. For actual execution of local fake calls
under controls, use the [LangGraph harness example](LANGGRAPH_HARNESS.md).

There are 24 planned observations and 23 recorded ones. Of four held-out
enforce-versus-shadow pairs, one passes quality, one is stopped and fails, one
lacks quality, and one is missing. All four stay in the denominator. The faster
stopped run appears in descriptive timing but cannot count as quality-preserving
improvement. Repeated runs of this fixture produce the same reports.

## Freeze the design before collecting outcomes

Use the existing application's runner with these four conditions:

| Mode | Configuration | Comparison |
| --- | --- | --- |
| `uninstrumented` | Original agent, no AgentLoop trace or policy wrapper in the measured path | Reference for trace overhead |
| `tracing` | Same agent with tracing, controls disabled | Tracing minus uninstrumented |
| `shadow` | Tracing and one policy evaluated without enforcement | Shadow minus tracing |
| `enforce` | Same policy/configuration with admission controls enabled | Enforce minus matching shadow; also enforce minus tracing |

Add policies individually before combinations. Each policy set must have a
matching shadow/enforce condition and single-policy conditions for every member.
Condition identity includes the declared policy version/configuration reference.
The report does not choose policies or promote a configuration automatically.

Build a specification with the fields below and freeze it with
`AblationProtocol.freeze(specification)`, imported from
`agentloop.ablation_protocol`. Save `protocol.to_dict()` before measurement, then
attach its `protocol_hash` to each observation. `from_dict()` verifies that the
saved specification still matches its digest. Returned dictionaries are copies.
Keep a dated external record of the frozen file: its hash and `frozen_at` field
alone cannot prove preregistration.

| Specification field | Required declaration |
| --- | --- |
| `schema_version` | `1.0` |
| `name`, `workload_id`, `permission_ref` | Study/workload identity and the owner's permission reference; the loader does not grant or verify permission |
| `frozen_at`, `synthetic` | Time with an explicit timezone, and a boolean fixture marker |
| `versions` | Immutable references for `source`, `model`, `provider`, `configuration`, `scorer`, `runner`, `environment`, `tools`, and `reset` |
| `tasks` | Task ID to `split` (`pilot` or `held_out`) and `input_sha256`; both splits are required |
| `conditions` | Condition name to `mode` and `policies` (policy ID to version/configuration reference); empty policies for off/tracing |
| `schedule` | Slots with `task_id`, string `repetition`, `cache_condition`, and an ordered list of all conditions |
| `quality_gate` | `min_score` and `max_regression`, each in `[0, 1]` |
| `bootstrap` | Positive `samples` up to 10,000, integer `seed`, and `confidence` strictly between zero and one |

Freeze the full model configuration, prompts, pricing snapshot, scorer code and
scorer configuration through those immutable references. Keep task input and
expected-answer artifacts outside the trace if sensitive. Hash the complete
task specification, including its constraints; predictable hashes are not
anonymization. Identical task digests cannot become separate task IDs. Represent
repetitions as separate slots under the same task. A task cannot cross splits.
Choose held-out task counts from pilot variability and the predeclared effect
size/quality tolerance; small examples remain exploratory.

The pairing key is `(task_id, repetition, cache_condition)`. A slot must contain
each condition once. Counterbalance condition order within split/cache strata;
the report exposes planned position counts and whether each condition's counts
differ by at most one across positions. That descriptive balance check does not
certify absence of carryover or provide an experimental design recommendation.
Declare cache preconditioning/reset procedures and analyze distinct cache states
separately. Provider seeds must be recorded when available; they do not guarantee
deterministic provider behavior.

## Record host observations without changing the agent

The host runner owns workload permissions, execution, external monotonic timing,
state snapshots/restoration, cache preparation, scorer calls, and spend limits.
Restore the declared environment before each condition. Measure the same span of
work in every condition; score results separately using the frozen scorer. Do not
derive the uninstrumented arm's latency from spans or fabricate a trace for it.
Record failed, timed-out, cancelled and interrupted attempts as well as successes.
Keep planned slots even when no observation survives.

Each observation contains these fields (see the example for a complete record):

- Identity/design: `observation_id`, `protocol_hash`, `task_id`, `repetition`,
  `cache_condition`, `condition`, zero-based `position`, `started_at`, actual
  `versions`, `reset_confirmed`, and optional `provider_seed` (use null if unknown).
- Outcome: `status` (`completed`, `failed`, `cancelled`, `timed_out`, `stopped`,
  `incomplete`), optional bounded `stop_reason`, `success` (boolean or null), and
  independent `quality_score` (number in `[0, 1]` or null).
- Metrics: `latency_ms`, total `tokens`, `cost_usd`, `model_calls`, `tool_calls`,
  `retries`, and aggregate `policy_eval_ms`, inside `metrics`. Unknown values are
  null. Counts are nonnegative integers; durations and costs are finite and
  nonnegative. Policy evaluation time can sum concurrent work and is not wall-clock
  overhead; use the matched condition differences to examine overhead.
- Provenance: `token_status`, `cost_status`, `cost_basis`, and `trace_run_id` (null
  when no trace is available). Use the existing token/cost status vocabulary;
  cost basis is `provider_reported`, `calculated`, `mixed`, or `unknown`.

These are host declarations. The reporter cannot verify that the runner used the
declared model, reset its environment, applied the intended policy, or ran an
independent scorer. Preserve runner logs and harness decision evidence for review.
Missing traces do not manufacture decision evidence. Do not put prompts, raw
answers, exception text or secrets in IDs, version references or stop labels.

Protocol-hash/version mismatches, observations predating the freeze, unconfirmed
resets and wrong order positions remain visible as protocol deviations. They
cannot contribute paired metric deltas. Unplanned observations are retained
without enlarging planned denominators. Duplicate pairing keys are ambiguous;
the reporter never selects the most favorable attempt. Duplicate observation IDs
are invalid artifacts.

## Interpret quality and measurements

The quality gate requires both observations to be completed, explicitly successful,
scored above the minimum, and within the declared regression allowance. Decimal
score/regression boundaries use the declared values without binary subtraction
rounding. A stopped run is conservatively excluded from quality-preserving
results even if a scorer says its partial output is acceptable. No-error spans
never substitute for task-grounded success. Missing quality is indeterminate.

Each comparison reports accepted, rejected, indeterminate and unmatched counts
against **all planned pairs**. `quality_acceptance_rate` is the observed accepted
fraction of that denominator; missing pairs are not asserted to be failed tasks.
Raw/descriptive metrics retain failed and stopped attempts. Separate
quality-preserving metrics use only gate-accepted pairs while retaining their
missing counts and full planned denominator. Neither view authorizes a universal
policy-benefit or quality guarantee.

Tokens contribute only with exact/empty provenance and an available count.
Provider-billed costs and calculated costs have separate summaries and intervals;
they are never pooled. Provider-reported complete charges do not require exact
token counts. Calculated costs require exact/empty token provenance as well as
complete pricing. Partial, unknown and mixed-basis charges remain in the raw
observations but cannot become zero or an evaluable paired cost. Cost savings do
not compensate for rejected or missing quality.

Deltas are candidate minus baseline. Reports separate pilot and held-out tasks
and cache strata. For each metric they show paired summaries and equally weighted
task means: available repetitions are averaged inside each task, then tasks are
weighted equally. A seeded percentile bootstrap resamples those task means, not
individual seeds. Fewer than two evaluable tasks produces `insufficient_tasks`.
Intervals describe the observed task sample under task exchangeability; they do
not fix selection/missing-data bias, scorer error, multiplicity, small-sample
coverage or workload representativeness. Metric-specific task/pair denominators
are retained in JSON and Markdown, including the quality-eligible subset.

## Link existing evidence

A bundle contains `schema_version: "1.0"`, the frozen `protocol`, the complete
`observations` list, optional `study_manifest` path, and `interventions` paths.
Paths resolve relative to the bundle. An empty observations list is valid and
reports every scheduled comparison as missing.
The CLI rejects resolved report paths that collide with each other or any bundle,
study, trace or ledger file it read, guarding against choosing an input as output.

The linked [study](STUDIES.md) uses only instrumented conditions and pairing keys
`["task_id", "repetition", "cache_condition"]`. Observation trace references must
match the study's condition and pairing metadata. Existing intervention records
are loaded unchanged; their source IDs and trace fingerprints must match that
study. Duplicate ledger artifacts are rejected. Unlinked study traces are listed
explicitly. Synthetic markers from linked traces/records propagate to the report
even if the protocol forgot its marker.

The embedded study remains descriptive. Its older pair-level bootstrap, if
requested in that manifest, is not the task-level ablation interval. Keep original
prediction snapshots before outcomes; do not regenerate them after observing a
candidate. Publishing a real case study also requires the permission-cleared
bundle, measured onboarding/instrumentation friction, retained regressions, and
the owner-reviewed task/scorer/spend protocol from
[#181](https://github.com/dipeshbabu/agentloop/issues/181).
