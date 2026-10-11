# Changelog

Changes to AgentLoop follow [Semantic Versioning](https://semver.org/).
The published version sequence starts at `0.1.0`; routine releases increment the
patch number, such as `0.1.1` and `0.1.2`. Package versions are separate from
trace, study, receipt, API and database schema versions.

## [Unreleased]

### Added

- Hermes 0.21.6 observer integration and a standalone directory-plugin exporter,
  with native turn traces, normalized request usage, retry and tool evidence,
  explicit incomplete capture, profile isolation, and opt-in output text.

### Fixed

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

[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.1.0
