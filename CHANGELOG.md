# Changelog

All notable changes to AgentLoop are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Project history from before the first public release remains available in Git.

## [Unreleased]

### Added

- Apache-2.0 licensing and open-source project metadata.
- Contributor, security, support, governance, release, and community conduct
  guidance.
- Structured bug and feature request forms, a pull request template, ownership
  rules, and automated dependency update configuration.
- A locked uv development workflow with pre-commit, PEP 8 checks, Ruff linting
  and formatting, and matching CI commands.
- Reproducible, lockfile-based uv installs in the official container build.
- Safer localhost-only Compose port bindings and explicit dashboard perimeter,
  TLS, request-limit, and trace-retention production guidance.
- Dashboard packaging in built distributions.
- CodeQL and dependency-review workflows for public-repository security checks.
- Local and CI security scanning with Bandit.

### Changed

- Replaced internal sales-planning documentation with contributor-focused
  dashboard and roadmap guides.
- Renamed value-report response fields from `sales_summary`,
  `estimated_customer_value_usd`, and `packaging_notes` to `value_summary`,
  `estimated_monthly_value_usd`, and `scenario_notes` before the first official
  distribution release.

### Fixed

- Corrected uv and CLI path examples across the README, dashboard, and
  deployment guides.
- Made OpenTelemetry exports report the package version instead of a hard-coded
  version string.

### Security

- Prevented authenticated project keys from selecting another project's API
  data and prevented an existing trace run ID from being reassigned across
  projects.
- Rejected executable custom Python scorers at the hosted API boundary and
  safely encoded trace identifiers in client request paths.
- Switched static and administrator API-key comparisons to constant-time checks.

[Unreleased]: https://github.com/dipeshbabu/agentloop/commits/main
