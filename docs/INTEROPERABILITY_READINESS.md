# Offline interoperability adoption and validation

AgentLoop reads completed Harbor/Omnigent evidence, qualifies findings and compares
externally scored outcomes. Harbor executes tasks and verifiers; Omnigent owns
harness sessions and policies. AgentLoop does not launch either runtime.

These are Unreleased checkout capabilities. The published `0.7.0` wheel predates
them. Feature work keeps the version, native trace/study schemas, storage/database
contracts, Python 3.10 minimum and core dependency list unchanged. Build and install
the intended checkout before using these commands; a release is a separate action.

## Reproduce the supported offline workflows

```bash
uv sync --locked --dev
uv run --frozen agentloop --help
uv run --frozen python examples/harbor_atif.py --out runs/adoption-atif
uv run --frozen python examples/harbor_trials.py --out runs/adoption-trials
uv run --frozen python examples/omnigent_otel.py --out runs/adoption-omni
uv run --frozen python examples/harbor_cross_harness_study.py --out runs/adoption-study
uv run --frozen agentloop study summarize runs/adoption-study/report/study.json --json-out runs/adoption-study/native-summary.json
uv run --frozen python examples/multiagent_coordination.py --out runs/adoption-coordination
uv run --frozen agentloop harness bench --adapter python-wrapped --mode offline --out runs/adoption-bench --json-out runs/adoption-bench.json
```

All examples use owned synthetic input files. They require no vendor credential,
Harbor/Omnigent installation, collector, sandbox, database or paid-model run.
Harbor/Omnigent upstream runtimes require their own Python 3.12+ environments when
used independently; that requirement does not extend to these file importers.

Harbor examples preserve legacy/v1.7/v1.8 format distinctions, source hashes,
subagent documents, copied context, aggregate/call multiplicity and unknown timing.
Completed-job examples retain failed, timed-out, cancelled, missing, invalid and
externally uninterpreted verifier cases. The paired example reports four verified
improvements out of five candidate attempts for the correct candidate, and zero
out of five for the faster incorrect candidate. Each condition's unidentified
planned attempt stays in its denominator. Synthetic scores and durations are
validation data, not empirical task-performance results.

Omnigent examples retain separate trace IDs, session correlation, source policy
observations and missing contexts. DENY/ASK spans establish no verified prevention
or approval. Missing usage and child timing remain unavailable. Coordination
reports preserve source document identities and inclusive usage separately, with
explicit coverage for observed overlap, unknown children and unsupported wait,
review and critical-path measurements.

The offline bench executes the existing wrapped Python boundaries and records
callable-side dispatch/stream/cancellation evidence. Its optional LangGraph path
requires the pinned SDK. Untested live model override, approval, fork and resume
features remain explicit skips. A tested wrapper does not establish vendor-wide
enforcement or a sandbox.

## Complete inventories and reproducibility

Every Harbor job export contains its original `harbor-inventory.json` plus
`harbor-trials.jsonl`. The latter writes one summary record followed by one record
per discovered trial, incrementally:

```json
{"schema_version":"1.0","record_type":"summary","summary":{"job_id":"recorded-or-calculated-source-identity","trials_discovered":1,"trials_planned":2}}
{"schema_version":"1.0","record_type":"trial","trial":{"trial_directory":"recorded-relative-directory","import_status":"missing_trajectory"}}
```

This illustrates record framing; actual records contain the complete existing
inventory summary and trial rows, including errors, verifier outcomes, pairing
metadata and receipt/native references. Planned unidentified attempts remain
summary gaps, with no fabricated trial IDs or execution traces. Consume JSONL a
line at a time; it is an inventory export, distinct from OTLP telemetry JSONL.

JSONL parsing/export and artifact comparison process bounded records/chunks.
Correlated import results retain native projections, receipts and inventory rows
under explicit aggregate limits; whole-job import is not constant-memory. Core
limits remain unchanged. Large synthetic measurements explicitly raise only
trajectory/reference caps and record every applied limit.

Repeated imports have stable receipt/native fingerprints. Repeated exports accept
identical bytes and reject conflicting files, later-record differences, trailing
content and indirect output references. Raised producer/write errors remove a new
partial artifact so the export can be retried; existing complete artifacts remain
unchanged. Both study writers check every generated condition folder before
exporting any condition, including dangling symlinks and junctions. These checks
assume a stable local tree and do not provide a concurrent filesystem sandbox.
When upgrading an adapter, select a fresh
output directory for changed projections. Source hashes establish supplied-byte
consistency, not authenticity, verifier integrity or provider billing.

## Evidence and supported scope

| Required scope | Implementation and validation path |
| --- | --- |
| Source receipts and bounded parsing | [INTEROPERABILITY.md](INTEROPERABILITY.md), strict receipt 1.0, traversal/symlink/numeric/depth/count limits and adversarial fixtures |
| Harbor ATIF | [HARBOR_ATIF.md](HARBOR_ATIF.md), direct v1.7/v1.8 and documented legacy v1.6 subset; no invented model calls, timing or parent usage |
| Completed Harbor jobs/verifiers | [HARBOR_TRIALS.md](HARBOR_TRIALS.md), config/locks/digests, independent declared thresholds, negative inventory and streamed row export |
| OTLP JSONL | [OTLP_JSONL.md](OTLP_JSONL.md), bounded records, trace/span identity, partial segments, duplicates, actual pinned converter outputs and inferred-time qualifications |
| Omnigent sessions/policies | [OMNIGENT_OTEL.md](OMNIGENT_OTEL.md), native OTel parsing, bounded aliases, noncausal links and unverified decisions |
| Capabilities | [HARNESS_CAPABILITIES.md](HARNESS_CAPABILITIES.md), declarations separated from verified/failed/skipped/unknown observations |
| Executable probes | [HARNESS_BENCH.md](HARNESS_BENCH.md), real wrapped dispatch/lifecycle counters, source proof hashes and pinned LangGraph conformance |
| Quality-aware cohorts | [CROSS_HARNESS_STUDIES.md](CROSS_HARNESS_STUDIES.md), full populations, exact task/config/scorer/repetition pairing, native studies and task-cluster uncertainty |
| Coordination | [MULTIAGENT_EFFICIENCY.md](MULTIAGENT_EFFICIENCY.md), source-bound actors/relationships, conservative timing coverage, model-leaf attribution and reuse abstention |
| Local artifacts | Existing native CLI/report paths plus source-qualified JSON/HTML, escaped text and shared offline styles/content policy |

Harbor model/converter contracts are frozen at
`d5ac1be17f575852eaf4fffc4072fd18481c209b`; Omnigent telemetry references at
`a2956be0e97bb175a60b274053d836f07d494c6c`. Initial AgentLoop baseline was
`69cfcb1cf82061fb70229465e296d821f650283d`. Fixture ownership and converter
provenance remain in the [external fixture corpus](../tests/fixtures/external/README.md).
These are pinned format proofs, not claims of live compatibility with every
upstream release or transport.

The final upstream refresh observed Harbor
`09f5b964f8e5075f7e52fd08c9994dfc80ad5d24` and Omnigent
`12a0d5c8737571980b84869c2df00b17d0b42c9b`. The
[Harbor delta](https://github.com/harbor-framework/harbor/compare/d5ac1be17f575852eaf4fffc4072fd18481c209b...09f5b964f8e5075f7e52fd08c9994dfc80ad5d24)
changed model-lookup documentation; the
[Omnigent delta](https://github.com/omnigent-ai/omnigent/compare/a2956be0e97bb175a60b274053d836f07d494c6c...12a0d5c8737571980b84869c2df00b17d0b42c9b)
changed native session/UI behavior. Neither changed the frozen Harbor model/ATIF
converter or Omnigent tracing/capability reference files. Supported format proofs
remain pinned; newer live runtimes were not exercised.

Optional P2 Harbor post-job plugins, active ASK resolution and conservative
research/RL export are deferred. They are not required for read-only adoption and
are not described as shipped. No provider/sandbox/live transport validation was
performed. API functions and CLI file imports supply the supported offline scope.

## Checks and next adoption experiment

```bash
uv run --frozen pre-commit run --all-files
uv run --frozen --all-extras python -m pytest -q
uv build
```

Focused PR validation additionally exercises newly built wheels in isolated
environments, verifies changed package bytes, tests offline commands and checks
Python 3.10/3.13/3.14, pinned LangGraph, package/standalone platforms, SQLite/Postgres,
replay and Docker deployment in CI. Final-head checks must pass before squash
merging; rewritten or retargeted heads require fresh evidence. Skipped automated
reviews are not approvals. No version bump or publication follows these checks.

Scalability measurements use the checked-in
[generator](../examples/interoperability_scalability.py) for 10/100/1000 trials,
sessions and model spans. It reports local CPU/wall time and phase-specific Python
allocation peaks, with repeated import fingerprints and export idempotence. Input
generation, interpreter startup and reproduction rechecks are outside measured
phases; lazy initialization may occur inside the first API call. Existing retained
objects are excluded from later phase allocation peaks. Whole-process RSS,
recording-disabled controls, agent instrumentation overhead and provider benefits
are not established by this parser benchmark.
The [measured results](INTEROPERABILITY_SCALABILITY.md) publish every observed
sample and the unchanged imported snapshots, including negative inventories.

For an adoption pilot, select a permission-cleared, pinned slice of independent
tasks and predeclare exact scoring, model/tool environment, pairing, missingness
and inclusion before execution. Run the benchmark externally, retain every trial,
counterbalance queue/cache/order effects and preserve held-out verification.
Import the completed artifacts, check coverage and compare paired quality before
considering performance. That live experiment needs separate authorization and
credentials; the offline examples do not substitute for it.
