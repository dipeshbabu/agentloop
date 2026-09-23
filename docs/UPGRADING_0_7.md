# Upgrade from 0.6 to 0.7

Version 0.7.0 ships the evidence workflow: canonical findings and estimator
provenance, intervention records, paired studies, offline HTML reports, telemetry
conformance, API v1, and the large-trace graph fixes. It does not add runtime
harness controls. The distribution remains `agentloop-profiler`; imports and the
CLI remain `agentloop`.

## Upgrade order

1. Back up the database and retain the previous package/deployment configuration.
   Test restoration before upgrading a production store.
2. Upgrade the server and dashboard to `agentloop-profiler==0.7.0` first. Initialize
   the configured store with `agentloop init-store`, then check `/readyz` and read
   an existing project trace before resuming writes.
3. Upgrade Python clients and CLI installations. The new client sends `/v1`
   requests and needs the upgraded server. Keep the configured server base URL
   free of a trailing `/v1`; the client adds it. Old clients can continue using
   unversioned routes throughout 0.x.
4. Update scorer configurations and explicit diagnosis writes as described below.
   Re-run your task-specific quality fixtures and CI gates on retained traces.

See [API v1](API_VERSIONING.md) for the route mapping. `/health` and `/readyz`
remain supported. Unversioned application routes are deprecated; their earliest
removal is 1.0 with a release notice.

Diagnosis GET requests now compute without persisting or superseding findings.
Use `POST /v1/traces/{run_id}/diagnosis`,
`AgentLoopClient.save_diagnosis(run_id)`, or `agentloop remote-diagnose` to save
recomputed findings. The legacy GET `/diagnose` alias is also read-only.

## Trace and token compatibility

Native traces now use schema **1.1**. Model events can record `token_provenance`
as `provider`, `tokenizer`, `user_supplied`, `estimated_words`, or `unavailable`.
Reports expose `token_status` independently of pricing completeness. Existing
schema 1.0 JSON remains readable without rewriting artifacts; missing provenance
is reported as `unspecified`, not promoted to provider usage.

For compatibility, cost gates continue evaluating legacy unspecified counts when
pricing is known. Counts explicitly marked estimated or unavailable make cost
gates indeterminate. They are non-failing by default, but fail a required positive
minimum cost improvement. Review `pricing_known`, `token_basis_evaluable`, and
cost/token status before interpreting a dollar delta as measured savings.
See [trace schema](TRACE_SCHEMA.md#token-provenance) and [pricing](PRICING.md).

Operation kinds are additive metadata; old event categories remain readable.
Estimator predictions now carry versioned inputs, coefficients, and assumptions,
and remain **uncalibrated**. An intervention gate pass is evidence only for its
configured checks and supplied pair, not proof of general recommendation quality.

## Quality scorer names

The misleading `regex` and `json_schema` scorer names are no longer accepted:

| Previous configuration | Replacement |
| --- | --- |
| `regex` | `glob`, `contains`, or `exact_match`, chosen for the intended comparison |
| `json_schema` | `required_fields` or `json_subset` |

These are semantic changes, not automatic renames. `glob` is bounded wildcard
matching, not a regular-expression engine. `required_fields` checks field presence,
not full JSON Schema validation. Rewrite and test fixtures accordingly; callers
needing a richer evaluator must supply and validate their own offline scorer.

## Database migration and recovery

The trace schema and database migration versions are separate. A 0.6 store already
has migrations 1 through 4. Version 0.7 adds migration **5**, `intervention_ledger`,
which creates the project-scoped `interventions` table in SQLite and Postgres.
Existing trace, finding, usage, and credential records are retained. Store
initialization applies pending migrations; repeated initialization does not
duplicate records.

For SQLite, use its backup API/`.backup` command or copy the database only after
stopping all writers. For Postgres, take a consistent `pg_dump` and verify that it
can be restored. Keep backups outside the active database path. Detailed commands
and deployment settings are in [production setup](PRODUCTION.md#database-schema-migrations).

There is no automatic down migration. If initialization fails, stop the rollout,
inspect the migration error locally, correct the reported cause, and retry store
initialization. Do not mark an unapplied migration as completed or drop evidence
tables to force startup. For rollback, stop writers, restore the pre-upgrade
backup, and redeploy the previous server/dashboard/client versions together.
Restoration discards post-backup writes; preserve those artifacts separately
before rollback when necessary.

## Verify the installation

In a fresh environment outside the source checkout, install the release and run:

```bash
python -m pip install agentloop-profiler==0.7.0
agentloop quickstart --out quickstart.json
agentloop analyze quickstart.json --html report.html --json-out analysis.json
```

The synthetic trace should yield findings and a self-contained HTML report. Follow
the [evidence workflow](EVIDENCE_WORKFLOW.md) for a reproducible study and ledger
example that retains a rejected quality outcome and unknown cost. These fixtures
test artifact behavior, not real-world performance improvements.

Release CI also runs the installed-wheel study and intervention smoke documented
in [the release guide](RELEASING.md). A merged release-preparation PR is not proof
of publication: verify the exact tag's validation, GitHub assets and `SHA256SUMS`,
and PyPI publication separately. The `pypi` environment approval remains required.
