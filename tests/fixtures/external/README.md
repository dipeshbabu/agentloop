# Synthetic external evidence fixtures

These fixtures are authored for AgentLoop. They contain no upstream user
sessions, credentials or copied upstream implementation code. The
[source matrix](source_matrix.json) pins inspected revisions, format/release
versions, expected behavior and supplied-byte SHA-256 hashes. Harbor and Omnigent
are Apache-2.0; fixture code/data follow AgentLoop's license. Structural fields
follow the linked upstream models and telemetry contracts.

See [the contract](../../../docs/INTEROPERABILITY.md) for provenance and gaps.
The `mixed_job` config/locks are field projections for data import, not complete
Harbor-executable configurations. OTLP examples are hand-authored protocol data,
not artifacts from Harbor's converter or an Omnigent vendor executor.

Fifteen families include untimed operations, omitted cost, shared sessions,
copied context, aggregated usage, absent trajectories, fractional rewards without
pass criteria, policy verdicts without enforcement, missing parentage, duplicate
segments and invalid JSONL. `hostile_cases.json` separates parser negative cases
from semantic adapter cases pending dependent workstreams.

Regenerate from the repository root only when updating frozen expectations:

```bash
uv run --frozen python tests/fixtures/external/build_fixtures.py
```

Example receipts retain unknown/null/absence. Their counters count receipts,
which must not be promoted into trial-level success rates.
