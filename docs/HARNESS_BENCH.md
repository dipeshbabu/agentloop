# Executable offline capability bench

The unreleased bench invokes the existing explicit Python/LangGraph adapters
with trusted local protocol fakes. It checks observed behavior and external
counters against [capability declarations](HARNESS_CAPABILITIES.md), then exports
versioned JSON/Markdown and per-probe proof files.

```bash
agentloop harness bench --adapter python-wrapped --mode offline --out runs/python-bench
agentloop harness bench --adapter langgraph-1.2.11 --mode offline --out runs/langgraph-bench
```

`--json-out` additionally writes the combined JSON report. Use an explicit
`--test-timestamp 2026-01-01T00:00:00Z` to reproduce byte-identical reports in the
same environment; default test metadata uses the actual UTC invocation time.
Policy timing uses a deterministic fake monotonic clock. Random internal trace/
call IDs are omitted from the normalized behavioral proof, never presented as
recorded external identities.

## What actually runs

| Probe | Required observed behavior |
| --- | --- |
| Basic sync/async | Typed return/argument identity, one dispatch and actual hook records |
| Tool observation | An explicit tool span and dispatched wrapped-call evidence |
| Pre-tool/pre-model DENY | Callable-side counter remains zero across sync/async/generator/async-generator; denial acknowledged before dispatch |
| Streaming | Lazy invocation; first chunk arrives before producer completion |
| Partial close | Closing after one chunk closes the producer without consuming the next chunk |
| Cancellation | A running async task starts, acknowledges cancellation and executes its cleanup; cancellation lifecycle recorded |
| Policy failure | Raised policy blocks dispatch and records failure; denial/stop/escalation is not a successful tool call |
| Repeated wrapping/errors | Existing idempotence plus original exception identity across all four callable lifecycles |

The protected callable owns the dispatch counter. A driver that executes first
and logs DENY afterward fails even when its native denial record looks correct.
A driver that buffers every chunk before yielding the first fails streaming.
Cancellation is coordinated with local events rather than wall-clock sleeps.

The protocol graph follows the four entrypoints used in existing integration/
enforcement conformance. LangGraph probes use the real pinned adapter/version
checks and signal classes when 1.2.11 is installed; they do not bypass missing
SDK checks or claim a fake is a complete compiled graph scheduler. Pinned real
graph conformance remains a separate hosted/local integration check.

## Scope and gaps

Proofs cover explicitly instrumented model/tool callable boundaries, not every
application operation. Hidden SDK work, vendor-internal tools and externally
running agents remain outside this coverage. Passing local async cancellation
does not prove a vendor process can be interrupted. The registry's universal
interrupt/pre-request dimensions remain unknown without relevant evidence.

Model override, approval, resume and fork are explicitly skipped with
`live_probe_required`. A fake requested model label cannot verify actual provider
selection. Read-only Harbor/Omnigent adapters have no runtime probe driver and
skip these probes rather than falsely passing runtime controls.

An uninstalled or wrong-version LangGraph runtime yields
`pinned_langgraph_runtime_unavailable`; it is neither a failed capability nor a
pass. No live/network mode is implemented. `--mode live` fails with an explicit
unsupported-mode error. No paid APIs, Docker, browser, vendor CLI, credentials
or global provider environment are needed for offline probes.

## Reports and drift

The report retains the original declaration, probe verdict, version/transport/
manifest identity, environment fingerprint, test timestamp and relative proof
reference/byte hash. Typed current in-process observations may qualify the
registry's verified states; deserialized historical reports remain external
claims until a fresh trusted probe runs.

Declared support with a behavioral failure yields `verified_failed` and drift.
Declared unsupported with a passing probe also produces drift, preserving the
original false declaration. Skips require reasons. Incomplete/stale/wrong-scope
results cannot verify support. JSON output reports `passed`, `failed` or
`skipped`; the CLI exits nonzero for probe failures or observed drift.

Output is stable with a fixed timestamp/environment, and conflicting existing
bytes fail instead of overwriting. Proof hashes bind supplied records, not
cryptographic honesty against malicious code in the trusted Python process.
Custom Python drivers are fully trusted test code; catalogs never discover or
execute them. The bench does not activate production policy or rewrite original
intervention predictions/outcomes.

## Verification

```bash
uv run --frozen python -m pytest tests/test_harness_bench.py tests/test_capability_registry.py tests/test_enforcement_conformance.py -q -ra
uv run --isolated --frozen --with langgraph==1.2.11 python -m pytest tests/test_harness_bench.py tests/test_langgraph_harness.py tests/test_enforcement_conformance.py -q -ra
```

The tests include dishonest denial and buffered stream drivers, both declaration
drift directions, real skips, deterministic records/hashes, no-network guards,
CLI mode rejection and current boundary qualification. Core Python 3.10 remains
supported; LangGraph is optional. Native schema, package version/dependencies and
existing harness behavior remain unchanged. This code is not in published 0.7.0.
