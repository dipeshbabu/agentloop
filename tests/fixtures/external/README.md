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

The additional `otlp/converter_*` fixtures are actual outputs from the pinned
Harbor converter applied to owned synthetic inputs, with their own
[provenance receipt](otlp/converter_provenance.json). They exercise v1.8 media,
inferred timing/status, aggregates, deterministic dispatch and embedded agents.
They do not claim a live Harbor job or alter the original fifteen-family matrix.

The job/trial importer also checked these field projections against Harbor
`d5ac1be17f575852eaf4fffc4072fd18481c209b` on 2026-10-09. Its trial/job result
fields and trial lock schemas 2/3 are documented in
[Harbor trial evidence](../../../docs/HARBOR_TRIALS.md). The original matrix
revision and frozen bytes remain the source of these synthetic fixtures;
neither revision is an inferred producer identity or a live execution claim.

The Omnigent adapter additionally checked these synthetic tracing projections
against `a2956be0e97bb175a60b274053d836f07d494c6c` on 2026-10-09. See
[Omnigent evidence](../../../docs/OMNIGENT_OTEL.md) for recorded aliases,
session/link gaps and policy-observation limits. Fixture bytes and their original
matrix provenance remain unchanged; no vendor execution or prevention is claimed.

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
