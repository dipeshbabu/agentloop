# Changelog

Changes to AgentLoop follow [Semantic Versioning](https://semver.org/).
The published version sequence starts at `0.1.0`; routine releases increment the
patch number, such as `0.1.1` and `0.1.2`. Package versions are separate from
trace, study, receipt, API and database schema versions.

## [Unreleased]

## [0.2.0] - 2026-11-12

### Added

- Reproducible guided repository, retrieval and tool-heavy pilot/held-out suite,
  with optional pinned local inference, real LangGraph execution, negative
  controls, native intervention/study artifacts and retained overhead controls.
- `improve init` generates a runnable offline example, independent task data,
  a sync/async Python adapter and a data-only project configuration.
- Guided plans show baseline quality, native finding rankings, estimate
  provenance, assumptions and missing evidence separately from measured results.
- Guided `improve capture`, `improve start`, and `improve finish` CLI workflow
  with fixed task inputs and quality checks, frozen baseline recommendations,
  content-hashed evidence, complete attempt inventories, native replay and
  intervention/study results, and readable Markdown/HTML comparisons.
- Reproducible Hermes repository-question workflow with pinned local CPU model
  inference and an incorrect-answer control evaluated through the guided CLI.
- Reproducible Hermes v0.22.7 runtime pilot and CI coverage for streamed requests,
  real file tools, retries, interruption, default privacy, and delegated child
  sessions, using an isolated profile and a deterministic local provider.
- Counterbalanced offline instrumentation controls retain recording, buffered
  JSON export and native analysis timings, with independent arithmetic checks,
  source provenance and failed/cancelled attempts.

### Changed

- Hermes observers accept `>=0.21.6,<0.24`. CI exercises pinned Hermes 0.22.7 and
  0.23.1 hosts with streaming, file tools, retries, cancellation, privacy and
  delegation. Evidence validation binds each run to one source pin.
  Published AgentLoop 0.1.1 retains its original Hermes 0.21.x compatibility range.

### Fixed

- Rejected guided comparisons also fail the linked intervention gates, including
  candidates with failed execution or no positive measured improvement.
- Guided CLI diagnostics identify failed tasks, missing traces and quality gates.
- Guided capture retains conventional child interrupt exits as cancelled attempts.
- Patch planning prunes generated build, dependency and environment directories,
  keeping recommendations scoped to application source.
- Installed-wheel validation exercises guided success and rejection paths outside
  the checkout. Local model studies run through an explicit manual CI input.
- Replay and CI comparisons reject incomplete, interrupted, or failed Hermes
  captures even when their shorter duration appears to improve performance.
- Schema-repair recommendations require a declared structured-output failure,
  preserving transport, rate-limit and unknown retries in the underlying metrics.
- TypeScript API requests bind fetch to its global receiver so native browser
  requests work. Installed-tarball checks exercise esbuild and Chromium,
  including concurrent spans, cancellation, privacy and redirect rejection.

## [0.1.1] - 2026-11-05

### Added

- TypeScript SDK for native model/tool/retry tracing and the API v1 routes, with
  explicit concurrent contexts, qualified usage, private credential scopes,
  cancellation, bounded responses, and Node/Python interoperability checks.
  The SDK is available from source; npm publication remains pending.
- Hermes 0.21.6 observer integration and a standalone directory-plugin exporter,
  with native turn traces, normalized request usage, retry and tool evidence,
  explicit incomplete capture, profile isolation, and opt-in output text.

### Changed

- Source builds require Python 3.11 or newer so all supported installations can
  use patched Pillow and filelock releases. Published 0.1.0 predates this change.

### Fixed

- Python 3.15 container installs use compatible binary dependencies and receive
  full CI coverage with released interpreters alongside the existing Python matrix.
- Updated Pillow, certifi, filelock, virtualenv, and setuptools to patched
  releases, with resolver floors to prevent vulnerable downgrades.
- Published optional-extra requirements and source dependency groups enforce
  security floors even when an installer does not read uv-specific constraints.
- Failed retry spans retain their error status, and exception formatting cannot
  replace the business failure when an exception's string conversion raises.
- Python API requests reject redirects, bound response reads, preserve credential
  scopes, and avoid exposing keys or error response bodies in diagnostics.
- TypeScript metadata copying validates and copies each value once, ignoring
  custom serializers and rejecting inherited values in sparse arrays.
- Package discovery prunes generated runs, SDK dependencies, tests, and build
  output before traversing their directory trees.
- Repository link checks exclude generated run artifacts, package smoke
  environments and JavaScript dependencies while retaining source documentation.
- CLI analysis, reports and study summaries reject destinations that alias source
  evidence or each other before writing, including symlinks and hard links.
- CI step-summary destinations are checked alongside reports so appending a
  summary cannot corrupt a trace, fixture or report.
- Release preparation restores the original version, changelog and lockfile bytes
  after a failed update or interrupted lock generation, so preparation can retry.
- Locked urllib3 and virtualenv dependencies now use patched versions for seven
  security advisories, with resolver constraints preventing vulnerable downgrades.

### Documentation

- Reorganized the README around installation, tracing, reports and quality gates,
  with executable examples and corrected availability notes for version 0.1.0.

## [0.1.0] - 2026-10-11

### Added

- Local tracing of model, tool and workflow operations, with dependency-free
  framework adapters, OpenTelemetry conversion and qualified usage provenance.
- Execution graphs, canonical findings, optimization estimates, local HTML and
  Markdown reports, sampling and incremental large-trace aggregation.
- Quality fixtures, replay gates, paired studies, intervention records, task-level
  uncertainty and reproducible offline optimization experiments.
- Optional dashboard, HTTP API, project-scoped authentication, SQLite/Postgres
  persistence, migrations, readiness checks and deployment scaffolding.
- Explicit wrapped-call harness controls, admission budgets, loop guards,
  completion checks, recorded decisions and optional pinned LangGraph adapters.
- Read-only Harbor ATIF/job/verifier and Omnigent telemetry imports, bounded OTLP
  JSONL, immutable source receipts, complete negative inventories and qualified
  session, policy, capability and coordination reports.
- Executable offline capability probes, external-quality cohort comparisons,
  synthetic examples and measured interoperability scalability results.
- Classified/routed/extracted AI workflow examples, frozen research evidence and
  calibrated finding evaluation with explicit source and measurement limits.
- Validated Python distributions and standalone CLI artifacts for Linux, Windows,
  macOS Intel and Apple silicon, with checksums and dependency notices.

### Fixed

- Interrupted artifact writes remove their new partial files so complete exports
  can retry. Existing and competing writers' artifacts stay unchanged.
- Harbor study writers validate every generated condition directory before
  exporting, including existing/dangling symlinks and junctions.

### Release process

- Release preparation validates the source and opens a reviewable version/changelog
  PR. Merging preparation does not automatically tag or publish a release.
- Explicit version tags validate the exact source commit before publishing the
  validated artifact bytes to GitHub and PyPI through trusted publishing.
- Optional live-provider validation, Harbor post-job plugins, active approval
  resolution and research trajectory export remain outside this release.

[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dipeshbabu/agentloop/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/dipeshbabu/agentloop/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.1.0
