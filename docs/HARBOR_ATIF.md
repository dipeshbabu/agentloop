# Harbor ATIF import

The Unreleased source checkout imports ATIF v1.7/v1.8 and a v1.6 compatibility
subset without Harbor, Docker, a provider SDK or network access. Import reads
data; it does not execute an agent or verifier. These commands are not in the
published `agentloop-profiler==0.7.0` wheel.

This implements the trajectory boundary of
[workstream #273](https://github.com/dipeshbabu/agentloop/issues/273). Job/trial
inventory, verifier outcomes, JSONL and paired studies remain separate work.

## Offline workflow

```bash
uv run --frozen agentloop harbor import-atif tests/fixtures/external/harbor/atif_embedded_subagents.json --out runs/harbor-import --synthetic
uv run --frozen agentloop harbor inspect-atif tests/fixtures/external/harbor/atif_embedded_subagents.json --json-out runs/harbor-inventory.json
uv run --frozen python examples/harbor_atif.py --out runs/harbor-example
```

The example writes three native traces, source receipts, inventory, analysis JSON
and self-contained HTML. The source documents share a session but keep distinct
run IDs. Token counts and optional source intervals are synthetic contract data,
not an observed provider execution or an optimization benefit.

Analyze any generated `traces/run_*.json` using the existing command:

```bash
agentloop analyze PATH --html report.html --json-out analysis.json
```

`report`, `diagnose` and `optimize` use the same qualified metrics and canonical
finding rules. Omit `--synthetic` for actual source data. `--root` selects the
allowed reference root, defaulting to the input directory. Local trajectory
references require `.json` and resolve relative to the source document; absolute,
remote, parent-traversal and symlink/junction/reparse paths are unsupported.
Media references are never opened. Use a stable artifact snapshot.

`--continue-on-error` preserves invalid primary documents as error receipts.
Malformed child documents remain error receipts in partial imports. Missing,
unsafe, mismatched and cyclic references stay unresolved. Bounds are terminal;
continue mode does not bypass them.

## Python API and identity

```python
from agentloop.integrations.harbor.atif import AtifOptions, import_atif

result = import_atif(
    "jobs/trial/agent/trajectory.json",
    root="jobs/trial",
    identity={"job_id": "recorded-job", "trial_id": "recorded-trial"},
    options=AtifOptions(),
)
result.write("runs/imported-trial")
print(result.inventory())
```

The internal result contains `traces` and immutable `source_receipts` tuples;
`warnings` returns detached source notices. Caller context is an explicit
declaration, not authentication. Missing document IDs use deterministic hashes
with calculated provenance. Seeds, attempts and task identities are not invented.

Repeated unchanged export preserves bytes. Conflicting existing output or
mutation after receipt capture raises an error. Receipt hashes bind the native
bytes that result exports. An inventory is not a native study manifest.

## Preserved semantics

| ATIF signal | Behavior |
| --- | --- |
| User/system/agent | Original role/order/IDs; not every step is inference |
| `llm_call_count=0` | Deterministic dispatch with no model call |
| `llm_call_count=1` | One attributed block |
| `llm_call_count>1` | One aggregate block plus multiplicity, no fake per-call spans |
| Missing count with model metrics | Evidence block with unknown physical call count |
| Agent text without inference evidence | Agent source record; model coverage unknown |
| Copied context | Sidecar history, no new execution or observation |
| Tools/observations | Call IDs and source-step association; missing results explicit |
| Cache usage | Subset of prompt count, never added twice |
| Final/root/child totals | Separate source aggregates, never added to step usage |
| Embedded children | Resolve by document ID, not shared session ID |
| Delegation/continuation | Relationships, not fabricated causal edges |
| Image/audio | Metadata and safe reference/hash markers, no binary reads |

Imported usage is `external_reported`, a non-exact, non-evaluable native grade.
Source cost stays in receipts; counts are not multiplied by a price table.
Qualified reports use `null` for missing input/output counts and separately
retain known partial sums. Native model-call counts count recorded blocks;
reported multiplicity is separate. Provider input context and generic retries
are not present in ATIF, so their metrics remain unavailable.

## Timing and reader compatibility

ATIF timestamps normally lack end times/durations. Native events require numeric
duration and `ok`/`error`, so incomplete projections carry compatibility
placeholders, `timing_available=false`, `source_status_available=false` and explicit
`unknown` timestamps. These are not zero-time measurements or successful runs.

The reserved trace namespace `agentloop.external`, version `1.0`, contains exactly
`schema_version`, `source`, `receipt_id`, `runtime_ms`, `event_timing_complete`,
`execution_status`, `usage_complete`, `reported_model_call_count` and
`comparison_eligible`. Events carry `external_evidence_schema: "1.0"` and
per-field availability. Missing/malformed qualifications fail closed.

The current reader applies qualifications before reporting. Unavailable latency,
status, usage, critical path and savings stay unavailable. Count-based findings
remain source-qualified; unsupported estimates/inputs are `null`. JSON, Markdown,
HTML and existing nullable storage preserve these values. Raw trajectories cannot
pass replay/study/value comparisons without a complete trial/quality adapter.

Use this checkout or a future release documenting qualification support.
Older readers can structurally accept native schema 1.1 while ignoring the
namespace and must not analyze these projections. Native serialization stays
compatible; semantic support requires the new reader.

### Optional source intervals

The adapter recognizes the explicitly documented AgentLoop convention inside
ATIF `extra`:

```json
{"agentloop": {"timing": {"started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z", "duration_ms": 1000}, "execution_status": "completed"}}
```

Root bounds describe that trajectory, model-step bounds that model block, and
tool-call bounds that tool operation. Supply them only when those exact intervals
were recorded. Standard Harbor ATIF is not assumed to emit this convention.
Timezone-free/incomplete intervals stay unknown; reversed/contradictory ones
fail. Retained intervals/status are externally reported, not proof of correctness.

## Privacy and limits

Messages, arguments, reasoning, token IDs, attachments and exception text are
omitted by default. Structural identities are bounded and can still be private.
Remote/absolute media paths are hashed. Unknown extensions are bounded/minimized;
omissions carry reasons. Known sensitive fields remain omitted.

Python-only opt-ins are separate: `capture_content`, `capture_reasoning` and
`capture_token_ids`. Content capture does not automatically enable the latter
two. Capture is bounded by the metadata limit; review opted-in data before sharing.

ATIF v1.0–v1.5, archives/remote artifacts, vendor restoration, provider-accounting
reconciliation, live/paid execution, ATIF export, runtime enforcement and task
verification are unsupported here. Hashes identify supplied bytes, not authenticity
of agent-writable trajectories. No source/verifier script executes.

## Provenance and checks

The adapter follows Harbor
[`07ad34000e4c481451b1a0ea30a4a09548d6de8b`](https://github.com/harbor-framework/harbor/tree/07ad34000e4c481451b1a0ea30a4a09548d6de8b/src/harbor/models/trajectories).
This is the inspected contract revision, not an inferred producer revision.
The [fixture matrix](../tests/fixtures/external/source_matrix.json) records owned
synthetic data, exact hashes and source versions. No upstream implementation code
was copied. Producer versions remain unknown unless recorded separately.

```bash
uv run --frozen python -m pytest tests/test_harbor_atif.py tests/test_interoperability_contracts.py tests/test_token_provenance.py -q -ra
uv run --frozen python examples/harbor_atif.py --out runs/harbor-example
```

These offline checks cover formats, legacy IDs, intervals, aggregates, copied
history, privacy, unsafe/cyclic references, missing data and installed-wheel
CLI/HTML. They do not establish live interoperability or empirical improvements.
