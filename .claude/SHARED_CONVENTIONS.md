# Shared conventions (read before any stream)

Every issue plan in this directory assumes the rules below. They come from
`CONTRIBUTING.md`, the PR template, and the CI workflows.

## Dev environment & checks

```bash
uv sync --locked --all-extras --dev      # set up / refresh the environment
uv run --frozen pre-commit install       # once, after first sync
```

Before opening a PR, the same commands CI runs must pass locally:

```bash
uv run --frozen pre-commit run --all-files    # Ruff lint+format, PEP8, Bandit
uv run --frozen --all-extras python -m pytest -q
uv build                                      # packaging must still work
```

CI also runs a large CLI smoke on Python 3.13 (see `.github/workflows/ci.yml`) and
builds the Docker image. Python support is **3.10 and 3.13** — code must run on both.
Line length is 100; Ruff enforces formatting.

## Architecture rule (do not violate)

Dependencies point **inward** toward the core data model. Core tracing, analysis,
replay, and export code must **not** import a framework SDK, FastAPI, Streamlit, or
require a network connection. Integrations adapt third-party objects only at their
boundary, import their SDK lazily, and are tested with protocol-compatible fakes —
never a live service.

## Compatibility surfaces

Treat these as public contracts. Changing any of them requires a documented migration
or fallback, round-trip/regression tests, and a changelog note:

- exported/native **trace JSON** (schema, field names, ID shapes);
- **CLI** output consumed by automation and exit codes;
- **HTTP** request/response shapes and status codes;
- **environment variables**; and
- **database records / schema**.

Keep existing serialized 0.4-era traces readable where practical.

## Both-backend rule

`agentloop/store.py` holds **two** implementations, `SQLiteTraceStore` and
`PostgresTraceStore`. Any persistence behavior change must be made in both and covered
by shared contract tests (see Stream A / issue #22). SQLite is for local/dev; Postgres
is the hosted backend from `docs/PRODUCTION.md`.

## Definition of done (applies to every issue)

- Behavior change has tests that fail before and pass after.
- Lint, full test suite, and `uv build` pass locally.
- The issue's own **Acceptance criteria** are each satisfied (they are copied into each
  plan — check them off).
- User-facing behavior documented; `CHANGELOG.md` `## [Unreleased]` updated.
- Compatibility impact called out in the PR (use the template's sections).
- Substantial AI assistance disclosed per the PR template.
- Close the issue with `Closes #<n>` in the PR body.

## Per-issue plan format

Each `issue-<n>-*.md` has: metadata, problem, key files (with the issue's line refs),
a step-by-step approach, the verbatim acceptance criteria, testing guidance, and
compatibility/risk notes. Line numbers are from when the issue was filed — confirm the
current location before editing.
