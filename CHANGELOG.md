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
- Original native identifiers on exported OTLP spans
  (`agentloop.native_event_id`, `agentloop.native_parent_id`, and, from the
  OpenAI Agents bridge, `agentloop.native_span_id` / `agentloop.native_trace_id`)
  so a remapped id stays diagnosable.

### Changed

- **OpenTelemetry import now preserves the full trace ID.** A valid imported
  OTLP trace ID keeps all 32 characters instead of being truncated to the last
  16, so it round-trips back out unchanged. **Compatibility:** the run ID for a
  trace imported from OTLP now has the shape `run_` + 32 hex characters (it was
  `run_` + 16). Code that assumed a fixed 20-character imported run ID should be
  updated; native (non-imported) run IDs are unaffected.

- Replaced internal sales-planning documentation with contributor-focused
  dashboard and roadmap guides.
- Removed the trace-consuming CLI commands' implicit `--autogen` fallback.
  Missing, unreadable, non-file, and malformed inputs now fail with a non-zero
  exit code. Generate synthetic traces explicitly with `agentloop demo` or
  `agentloop demo-all`; generated traces are labeled and marked as synthetic.
- Renamed value-report response fields from `sales_summary`,
  `estimated_customer_value_usd`, and `packaging_notes` to `value_summary`,
  `estimated_monthly_value_usd`, and `scenario_notes` before the first official
  distribution release.
- Replaced caller-supplied regular-expression quality scorers with bounded glob
  scorers. Raw `regex` scorers are now rejected; use `glob`, `contains`, or
  `exact_match`.
- **Renamed auto-instrument to integration detection.** `auto_instrument()` never
  enabled instrumentation — it only detected installed frameworks — so it is now
  `detect_integrations()` and the CLI command `auto-instrument` is now
  `detect-integrations`. Result fields `enabled`/`skipped` are renamed to
  `available`/`unavailable`, and detection uses `importlib.util.find_spec` instead
  of importing the frameworks. **Compatibility:** `auto_instrument()` and the
  `auto-instrument` command remain as deprecated aliases that emit a
  `DeprecationWarning`; `InstrumentationResult` remains as an alias of
  `DetectionResult`. Update code that read the `enabled`/`skipped` keys.

### Fixed

- Optimization plans no longer double-count savings from overlapping cards.
  Cards sharing affected spans are treated as mutually exclusive alternatives:
  plan totals now come from the compatible (span-disjoint) subset of cards that
  maximizes latency savings, breaking ties by cost savings, capped at the run's
  actual runtime and cost. `latency_reduction_pct` can no longer exceed 100%,
  and the reported latency/cost totals are always achievable by one concrete
  set of changes. Plans gain a `savings_aggregation` block recording the rule,
  the selected card indexes, and raw versus effective totals. The optimization
  queue applies the same rule per run and cluster, so `priority_score` consumes
  deduplicated savings. Compatibility: per-card estimates and all existing plan
  fields are unchanged; only the aggregated `estimated_after` totals (previously
  inflated), the queue's savings totals, and derived priority scores change,
  and `savings_aggregation` is a new additive field.
- Measure trace runtime as end-to-end elapsed time, retain cumulative span work
  separately, and calculate execution order and critical paths from timestamps
  and parent relationships.
- Remote CLI commands now honor environment-based user and administrator API
  keys while keeping administrator credentials out of ordinary requests.
- Corrected uv and CLI path examples across the README, dashboard, and
  deployment guides.
- Made OpenTelemetry exports report the package version instead of a hard-coded
  version string.
- OpenTelemetry export now emits valid trace and span identifiers for custom
  run/event IDs. Non-hex, wrong-width, empty, or all-zero native IDs are mapped
  deterministically (SHA-256) instead of being padded/truncated, so distinct IDs
  no longer collide (e.g. `a` and `0a`) and conforming OTLP consumers no longer
  reject the payload. Already-valid IDs are preserved unchanged. The native
  export and the OpenAI Agents bridge share one implementation.
- Escaped trace-derived content in every Markdown exporter with context-specific
  handling for headings, tables, inline code, fenced code, and raw HTML.

### Security

- Prevented authenticated project keys from selecting another project's API
  data and prevented an existing trace run ID from being reassigned across
  projects.
- Rejected executable custom Python scorers at the hosted API boundary and
  safely encoded trace identifiers in client request paths.
- Switched static and administrator API-key comparisons to constant-time checks.
- Store newly issued API keys with uniquely salted scrypt hashes instead of
  unsalted SHA-256. Keys issued by earlier pre-release builds must be rotated.
- Constrained patch-plan source discovery to a normalized allowed root and
  excluded source and directory symlinks from scans.

[Unreleased]: https://github.com/dipeshbabu/agentloop/commits/main
