# From a finding to an intervention study

AgentLoop's evidence workflow connects a recorded run, its findings, an applied
change, and the observed outcome. This example exercises the full local path
without model services, a hosted account, or a dashboard:

```bash
uv run python examples/intervention_study.py --out runs/evidence-workflow
```

It writes twelve deterministic synthetic traces across three tasks and two seeds,
six linked intervention records, original diagnoses and requests, one HTML report,
and paired study JSON/Markdown. Five candidate comparisons pass the configured
performance and quality gates; one fails quality and has indeterminate cost.
Rerunning the unchanged example returns the same ledger records.

These fixture outcomes verify the workflow. They do not measure recommendation
precision, establish performance gains for a real application, or calibrate an
estimator.

## Follow the artifacts

| Question | Artifact and fields |
| --- | --- |
| What happened in a run? | `baseline/*.json`, `candidate/*.json`: events, timing, usage, status, operation metadata |
| What looks wasteful or unreliable? | `diagnoses/*.json`: finding type, title, rewrite guidance |
| What evidence supports it? | Finding `affected_spans`, `evidence`, and declared/inferred evidence level; `example.html` links to those spans |
| Which assumptions are heuristic? | Finding `estimate`: method, uncalibrated flag, formula, coefficients, input snapshot, assumptions |
| What change was made? | `interventions/*.json`: intervention type/configuration, target findings, baseline/candidate IDs |
| What did AgentLoop predict? | Intervention `predicted.findings`: original recommendation snapshots |
| What actually changed? | Intervention `measured.deltas` and `measured.quality`: latency, tokens, cost, calls, retries, and quality |
| Did the change pass its checks? | Intervention `gates_passed`, gate configuration/results, and quality failures |
| How often did this recommendation meet the chosen acceptance rule? | `outcomes.json`: configured gate pass count/rate for this intervention family; `study-results.json`: paired effects and metric denominators across tasks/seeds |

The demonstration changes three serial lookups to concurrent lookups. It records
that change in the intervention configuration and compares a pair of supplied
traces. In your application, instrument and run both conditions yourself;
AgentLoop neither applies the code change nor executes it for you.

## Use your own data

1. Record representative baseline runs with task IDs and repetition/seed metadata.
   Review each diagnosis and preserve its estimator snapshot before changing code.
2. Implement the selected intervention and record the candidate with the same
   pairing metadata and a distinct run ID. Keep input data and model/configuration
   choices comparable or record differences explicitly.
3. Configure quality and performance gates for your task. Create an
   [intervention record](INTERVENTIONS.md) from the stored findings and pair,
   retaining failed and indeterminate results alongside successes.
4. Define a [study manifest](STUDIES.md), inspect unmatched/ambiguous runs, and
   summarize paired effects. Report the acceptance rule and denominator when
   calculating a useful-intervention rate. A rate for one task sample and gate
   configuration is not a general precision estimate.
5. Share an [HTML report](HTML_REPORTS.md) with the original JSON artifacts,
   source revision, configuration, and evaluation fixtures. Review private data
   before publishing. HTTP users can retrieve evidence through [API v1](API_VERSIONING.md).

The example's small bootstrap illustrates the output contract only. Repeated
seeds within tasks may need cluster-aware analysis, and missing costs or quality
scores limit the conclusions. Empirical calibration from accumulated real
interventions remains future work.
