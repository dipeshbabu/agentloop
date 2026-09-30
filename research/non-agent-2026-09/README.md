# Non-agent workload evidence, September 2026

The [report](REPORT.md), [machine-readable results](results.json) and
[frozen protocol](protocol.json) retain actual learned-model executions on three
public-data research applications. There are 96 fitting and 384 held-out paired
comparisons, with original predictions recorded before candidates, selected and
rejected findings, recording-disabled controls, quality losses and unknown costs.

These are numeric CPU applications, not autonomous agents or customer production
traffic. The [methodology](../../docs/NON_AGENT_STUDY.md) describes owner-delegated
criteria, identity/feature-disjoint partitions, actual learned models and limits.
[Dataset attribution](DATA_LICENSES.md) records official sources, CC BY 4.0 terms,
transformations and identifier omissions.

Batching preserves all baseline predictions. It meets the frozen quality floor
and non-regression criterion on 64/64 banknote, 64/64 matching and 48/64 bean
held-out pairs; the remaining bean pairs already had inadequate baseline quality.
Cheaper candidates retain 52 banknote, six matching and six bean paired quality
regressions. The bean candidate's improved aggregate accuracy does not hide its
pair-level regressions. Actual inference incurred no provider charge; operating
cost remains unmeasured and language-token usage is unavailable/not applicable.

Native feedback includes 192 first-repetition held-out finding-label cases and
1,944 selected/rejected calibration registrations. Isolated banknote/matching
batching outcomes support descriptive latency diagnostics. Combined configurations
are explicitly withheld from per-finding attribution, with their ledgers/outcomes
retained and exclusion reasons recorded. No estimator coefficient is installed.

Protocol hash:
`fd0956a9ff584b3c378f5a6b36a6865ec2093e1b3c0f4038340610d698e123cf`.
Result hash:
`f92cf5f36451e1929331e0710561d2fa9527752b5f27cb88de7f6a5a4f32b42d`.

The [multipart index](index.json) identifies checksum-verified compressed transport
parts. Use the study's bounded restorer because large logical JSON artifacts are
compressed and split into chunks inside the transport:

```bash
python -m examples.non_agent_study.package unpack research/non-agent-2026-09/index.json --out /tmp/nonagent-evidence
python -I /tmp/nonagent-evidence/implementation-reproduction/examples/non_agent_study/reproduce.py \
  /tmp/nonagent-evidence --json-out /tmp/nonagent-rebuilt.json --markdown-out /tmp/nonagent-rebuilt.md
```

The execution snapshot is preserved separately from corrected export/reproduction
code. Completing packaging and enforcing the already-declared attribution scope
did not rerun or replace model observations. Restoration verifies every logical
file, rejects unsafe/duplicate paths and caps logical output at 512 MiB. Output
directories must be new. Offline reconstruction invokes no fitting, inference or
external scorer. Native calibration comparison permits only relocation of its
absolute source-path list and corresponding outer hash; all registrations and
statistics must match.

Actual inference used Python 3.13.12 and NumPy 2.5.1. Selected training/task data,
learned JSON parameters, environment/setup/warm-up records, raw traces, quality,
native ledgers, label corpora and calibration inputs are retained. Full original
archives remain available at their pinned public URLs. Dependency implementations
are not redistributed; install the retained environment dependencies for reruns.
Different hosts need not reproduce wall-clock timings exactly.
