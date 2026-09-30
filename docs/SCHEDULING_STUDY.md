# Declared read-only scheduling study

This study runs four independent SQLite queries per task through the
[declared tool scheduler](TOOL_SCHEDULING.md). Each callback opens and closes its
own read-only connection to frozen public UCI Wine Quality data. Dependency,
resource and concurrency declarations are explicit; no model is used and no
artificial delay is inserted to produce a speedup.

All 96 observations preserve the independently frozen ordered outputs, covering
384 actual queries. Parallel enforcement reaches four concurrent callbacks in
every held-out scheduled observation. It is faster than the corresponding shadow
scheduler, but the complete adapter remains slower than tracing alone on these
small local operations.

## Results

| Held-out condition | Correct task batches | Mean task time | Peak scheduled callbacks |
| --- | --- | --- | --- |
| `off` | 8/8 | 7.128 ms | Sequential host execution |
| `trace` | 8/8 | 7.205 ms | Sequential host execution |
| `shadow_schedule` | 8/8 | 11.313 ms | 1 |
| `enforce_schedule` | 8/8 | 8.084 ms | 4 |
| `shadow_budget` | 8/8 | 12.650 ms | Sequential host execution |
| `enforce_budget` | 8/8 | 12.614 ms | Sequential host execution |
| `shadow_combined` | 8/8 | 14.031 ms | 1 |
| `enforce_combined` | 8/8 | 11.738 ms | 4 |

Scheduler enforcement minus shadow averages -3.229 ms, with a task-bootstrap
interval of [-3.685, -2.460] ms. Enforcement minus tracing alone averages
+0.879 ms, with an interval of [0.161, 1.597] ms. The latter retains the full
control overhead rather than presenting the favorable shadow comparison as an
overall speedup. The combined scheduler/budget path is also slower than tracing
alone. The four-call budget never binds on a successful four-query task.

All observations completed; there were no failed, cancelled or missing real
attempts. Offline tests separately cover those paths, including partially completed
mutations and budget races. There are zero model calls and zero model tokens.
Native model-cost totals are empty/zero because no model was invoked; whole-task
operating cost remains unknown in the ablation. No paid provider was used.

## Frozen design and limits

Six task batches contain two pilot and four held-out cases, with two repetitions
and eight conditions: 96 observations, including 64 held-out observations. Every
held-out condition occupies every order position once. Pilot ordering covers only
four cyclic positions. Task inputs, reference query results, database/source hashes,
policy versions, environment and execution code are frozen before measurement.

The native ablation includes off, tracing, separate shadow/enforce scheduler and
budget arms, and their combination. Every callback declares no prerequisites,
read access to the same dataset, no writes, read-only effects and worker-thread
support. The four queries use separate SQLite connections; sharing a thread-bound
connection would not satisfy that declaration. URI read-only mode, `query_only`
and a restrictive authorizer reject writes and database attachment.

Each observation creates fresh requests, scheduler and harness state. Timed work
includes callback construction, control setup, thread-pool setup, connections,
queries, result collection, cleanup and tracing where enabled. Dataset creation,
reference grading and artifact export are outside timing. Filesystem/SQLite
caches remain warm, and the machine is shared. The unchanged database checksum
is verified after execution by the plan loader.

The scorer checks call IDs, submission order and complete typed query results
against reference results computed before the measured runs. The same
SQLite implementation computes the references; this evaluates orchestration
and output preservation, not independent correctness of the database engine.
Four held-out batches and one host do not support a general performance claim.
Worker contention can increase summed callback time even while wall time falls.

## Evidence and reproduction

The [checksummed evidence index](../research/tool-scheduling-2026-09/index.json)
retains all 96 receipts, 84 native traces, declared and actual schedules, budget
decisions, frozen database/CSV data, source snapshots and native ablation/study/HTML
reports. The twelve uninstrumented observations have host receipts without
fabricated spans. Operating-cost uncertainty is preserved separately from the
native recorded-model accounting scope.

The exporter rechecks slot identity, ordered outputs, independent grades, tool
counts, schedule limits, trace hashes, dependency validity and agreement between
host and native control evidence. Partial/empty exports keep planned denominators;
unexpected attempt artifacts require review. No estimator-ledger attribution is
invented for this caller-configured dependency/resource plan.

```bash
python examples/real_agent_study/archive.py research/tool-scheduling-2026-09/index.json --out /tmp/scheduling-evidence
python -m examples.scheduling_study.export --plan /tmp/scheduling-evidence/study/plan.json --out /tmp/scheduling-report
```

Use a complete core checkout at `33646da91515bbf0add2f060ee5dbecebc8d75f4`
with the archived source overlay. Readers/exporters execute no tool callbacks.
For new measurements, prepare a fresh root with `examples.scheduling_study.protocol`
and explicitly execute `examples.scheduling_study.runner --plan PLAN --execute`.
Changed declarations, code, data or grading rules require a new frozen protocol.

UCI Wine Quality remains CC BY 4.0 with attribution in the bundle; study code is
Apache-2.0. No model weights, inference runtime or external side effects are
needed for this workload. No result activates a production schedule.
