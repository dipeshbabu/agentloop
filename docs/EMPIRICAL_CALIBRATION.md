# Empirical calibration with attribution and failures retained

The September 2026 calibration artifact contains 184 original finding
registrations. It preserves the earlier agent-routing outcomes without assigning
one combined intervention's effect to each of its findings. A separate
single-decision experiment supplies attributable latency observations, but none
of its candidate outcomes preserves quality. No runtime coefficient or policy
is changed, and the fitted diagnostic factor is not a deployment recommendation.

## Inventory and attribution

| Source | Registrations | Treatment |
| --- | ---: | --- |
| Revised #181 pilot and held-out routing study | 168 | 98 selected findings from 42 combined interventions; 70 rejected findings retained without claimed outcomes |
| New single-decision experiment | 16 | One original routing finding per intervention; four fitting pairs and twelve held-out pairs |

All source traces, original predictions and immutable ledger records are retained
in the [checksummed evidence bundle](../research/empirical-calibration-2026-09/index.json).
The native report has nine separate estimator/workload/configuration cohorts.
No synthetic contract fixtures enter empirical fitting.

The 42 earlier interventions replaced all model steps and contain two or three
original findings each. The existing calibrator marks their 98 selected
registrations `combined_intervention_unattributed` and `duplicate_outcome`.
These labels mean the same combined outcome cannot be credited repeatedly;
they do not mean 98 independent executions occurred. The records are neither
split nor rewritten to force attribution. Other findings remain rejected with
their selection reasons and original predictions visible.

The retired first onboarding protocol remains available in #181's bundle and is
outside these revised-workflow cohorts. The #188 count-cap study has no matching
recommendation-based estimator attribution and is not fitted here. Inventory
completeness applies to the explicitly named cohorts, not every possible workload.

## Attributable experiment

The additional workload is one model decision that returns a numeric answer to
a public GSM8K question. It is not another autonomous-agent benchmark. Eight
questions are chosen by a fixed hash order, excluding indices used in #181:
two fitting tasks and six held-out tasks, each repeated twice under both models.
Exact bounded numeric normalization supplies independent quality labels.

The baseline and candidate use the same pinned model artifacts as #181, with
temperature zero, disabled prompt caching and a 32-token output cap. This
short-answer constraint is part of the tested configuration, not a claim about
either model's general mathematical ability. Each trace has one model call and
one selected `route_to_smaller_model` prediction. The original finding is saved
with a post-write timestamp journal before any paired candidate runs. Outcomes
receive a separate post-write journal after the native ledger is saved.

All 32 attempts and 16 interventions are retained. The baseline answers 6/16
correctly; the candidate answers 0/16 correctly. Six candidate responses are
malformed/truncated under the output cap; other unsuccessful answers remain
completed-but-incorrect outcomes. The native independent quality gate rejects
all 16 pairs. Failed outputs are not removed from raw latency calibration.

## Raw latency diagnostics

The original estimator is `route_to_smaller_model` version `1.0`, with the saved
heuristic `sum_duration_ms * latency_fraction` and latency fraction `0.25`.
The native diagnostic fit averages repetitions within each fitting task, then
fits a multiplicative factor through the origin. Its factor is **1.258062**,
based on only two fitting tasks. That identifiability floor is not adequate
evidence for general calibration.

| Split | Independent tasks / pairs | Mean original prediction | Mean realized raw savings | Mean original signed error | Mean scaled signed error |
| --- | --- | --- | --- | --- | --- |
| Fit | 2 / 4 | 289.920 ms | 381.827 ms | -91.907 ms | -17.090 ms |
| Held out | 6 / 12 | 301.081 ms | 253.683 ms | +47.398 ms | +125.096 ms |

Signed error is prediction minus realized savings; realized savings is baseline
minus candidate latency. The factor shifts held-out mean bias farther positive.
The original held-out signed-error interval is approximately -294.242 to
414.101 ms. Intervals resample task means, not repetitions as independent tasks.
Small samples, fixed condition order, shared hardware and public-benchmark
training overlap limit interpretation.

These are **raw resource** diagnostics, including wrong and malformed answers.
The quality-preserving subset has zero eligible observations. No cost factor is
fitted: self-hosted operating cost is unavailable for every run. Zero paid-provider
spend is recorded separately and is not substituted for measured operating cost.
The original predictions, coefficients and ledger contents remain unchanged.

## Time provenance and freshness

For the earlier study, original archive-file last-write times were captured from
the retained execution directory and checked to precede candidate starts. Their
prediction hashes match the already-published evidence. These filesystem times
are disclosed provenance, not independent proof of preregistration. If only a
relocated archive is available, the inventory builder leaves unrecoverable times
unknown rather than backdating them.

The new cohort uses explicit post-write journals. The artifact's declared `as_of`
is `2026-09-24T01:34:40.318364+00:00`, with `valid_until`
`2026-10-01T01:34:40.318364+00:00`. This historical window does not refresh when
the report is read and never authorizes activation. Any model, provider,
workload, scorer, policy, environment or quality-gate change requires renewed
evidence even within that window.

## Reproduce without model calls

```bash
python examples/real_agent_study/archive.py research/empirical-calibration-2026-09/index.json --out /tmp/agentloop-calibration-evidence
agentloop study calibrate /tmp/agentloop-calibration-evidence/calibration/manifest.json --out /tmp/calibration-report.md --json-out /tmp/calibration-report.json
```

Use the recorded AgentLoop source/version. The native calibrator reads saved
predictions and outcomes; it does not rerun today's estimator, reprice historical
calls, execute models or modify policies. Relocation changes source-path fields
and the artifact hash, but numerical results and cohort decisions are reproducible.
Use the frozen manifest for reproduction rather than inventing archive timestamps
from newly extracted files.

`examples/real_calibration_study.py` prepares and explicitly executes new local
single-decision trials. `examples/empirical_calibration.py` builds the historical
inventory and delegates all diagnostics to the existing native calibrator.
The model and data sources retain the [licenses and attribution from #181](REAL_AGENT_STUDY.md#sources-and-permissions).
GSM8K's MIT license is included. No model weights or inference binaries are
redistributed, and required CI makes no model calls.
