# Changelog

All notable changes to AgentLoop are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Project history from before the first public release remains available in Git.

## [Unreleased]

### Added

- **Provider-aware model cost estimation with an explicit unknown state.** The
  cost calculator now recognizes OpenAI, Anthropic, and Google models, resolves
  provider prefixes (`openai/gpt-4o`) and dated snapshots (`gpt-4.1-2025-04-14`
  → `gpt-4.1`), and records pricing provenance (`source` and an `as_of` date) on
  every calculated cost. A provider-reported cost supplied on an event's
  `metadata` (`provider_reported_cost_usd`) is used verbatim, and cached-input
  tokens (`cached_input_tokens`) are billed at a model's cached rate where one
  exists. Configure rates for your own, local, fine-tuned, or newer models
  without editing the package via the `AGENTLOOP_PRICING_FILE` JSON file or a
  programmatic `PricingTable` — see the new `docs/PRICING.md`. Trace reports gain
  a `cost_breakdown` object distinguishing calculated, provider-reported, and
  unavailable cost, with per-model detail and the pricing sources/dates used, and
  a first-class `cost_status` (`complete` / `partial` / `unknown` / `empty`)
  propagated to the optimization plan, value report, and replay report so a
  lower-bound total is never mistaken for an exact one. An explicit provider is a
  hard resolution constraint (`azure/gpt-4o` will not borrow the OpenAI rate),
  rates can declare a `max_input_tokens` context ceiling (Gemini 2.5 above 200K
  resolves to unknown rather than under-reporting), and non-standard billing
  modes (`batch`, `priority`) require a `model#mode` rate or resolve to unknown.
  Untrusted event metadata can no longer crash report generation: a non-finite,
  negative, or non-numeric provider-reported cost yields an `unknown`
  (`invalid_metadata`) estimate, and cached-input tokens are clamped to the input
  count. When cost is incomplete, replay's `cost_usd_delta` /
  `cost_improvement_pct` / `cost_regression_pct` are `null` (rendered
  `unavailable`) instead of lower-bound arithmetic.
- Added a repository-owned CodeRabbit configuration for automatic, incremental
  pull-request reviews focused on correctness, security, compatibility, and
  regression coverage, while leaving merge authorization to CI and human reviewers.

### Changed

- **Unknown model pricing is now explicit instead of a fabricated default.**
  Previously every unrecognized model was silently assigned a generic `$1/M`
  input, `$3/M` output rate, so a Claude, Gemini, local, fine-tuned, or newly
  released model produced a plausible-looking but invented cost that flowed into
  regression gates, recommendation priority, modeled monthly value, and suggested
  pricing. A model with no known rate now reports **unknown** cost (`amount_usd`
  is `null`) and contributes nothing to the trace's `estimated_cost_usd`, which
  is now the sum of *known* cost only. **Compatibility:** `estimated_cost_usd`
  stays a float and is unchanged for recognized models, but drops for any trace
  that previously relied on the fabricated default — this is the intended
  correction; when `cost_breakdown.has_unknown_cost` is true, treat
  `estimated_cost_usd` as a lower bound. `agentloop.costs.estimate_cost_usd()`
  now returns `float | None` (`None` for unknown models) instead of always a
  float, and `MODEL_PRICES` no longer has a `default` entry.
- **Replay/CI cost gates define unknown-cost behavior instead of comparing
  coerced zeros.** When either trace has an unknown model cost, the
  `cost_regression` and `cost_improvement` gates are marked `indeterminate` and
  the report sets `gates.cost_evaluable = false`. By default an indeterminate
  cost gate does not fail the replay (a latency-only optimization using an
  unpriced model is not blocked), but a *required* cost improvement
  (`min_cost_improvement_pct > 0`) fails when it cannot be verified. Price the
  models via `AGENTLOOP_PRICING_FILE` to make the gates evaluable.

### Fixed

- OpenAI Agents SDK spans now propagate failure status, messages, and safely
  normalized structured error context into AgentLoop events.
- Quality fixture validation now rejects empty or vacuous suites and invalid
  score ranges across the library, CLI, HTTP API, and dashboard. Fully
  fail-closed replay/CI behavior remains tracked in #12.
- Release-prep CI runs started through `workflow_dispatch` now upload, download,
  and byte-verify the built wheel and source archive, matching pull-request CI
  and leaving artifacts available for maintainer review.

### Security

- Restricted Python client credentials to their intended endpoints. Ordinary requests
  now send only the project API key, while API-key creation sends only the admin key,
  even when `AgentLoopClient.from_env()` loads both credentials.
- Upgraded release artifact downloads to the native Node.js 24 action runtime
  and added a checksum-preserving upload/download smoke test for the exact
  distributions passed to the publishing job.

## [0.5.0] - 2026-07-18

### Added

- **Versioned database migrations and a shared SQLite/Postgres contract test
  suite.** `agentloop/migrations.py` replaces the implicit `CREATE TABLE IF NOT
  EXISTS` schema evolution with an ordered, idempotent migration list tracked in
  a new `schema_migrations` table; a database that predates this system is
  recognized as already at the pre-migration baseline, so upgrading is
  automatic on the next `init()` (which every store call already triggers).
  `tests/test_store_contract.py` and `tests/test_migrations.py` run the same
  assertions — init, API keys, upsert/conflicts, idempotent usage, findings,
  filters, pagination, the finding lifecycle, concurrent `init()`, connection
  failures, and fresh-vs-upgraded schema equivalence — against both backends;
  CI now runs a pinned Postgres service so the Postgres half actually executes
  instead of being silently skipped. See `docs/PRODUCTION.md` for the
  backup/upgrade/rollback procedure.
- **Pagination for trace and finding list APIs.** `GET /traces` and `GET
  /findings` accept `page_size` (max 200) and an opaque `cursor`; responses
  include `next_cursor` (`null` on the last page). `list-stored-traces` and
  `list-findings` gained `--limit`/`--cursor`, printing the next cursor when
  more rows are available. The dashboard's Traces page gained a page-size
  control with Previous/Next navigation. `optimization_queue()` now considers
  a bounded window of the most recently updated open findings per project
  (default 5000) instead of a project's entire finding history.
- **Finding lifecycle: `detected` → `accepted` → `resolved`, plus `dismissed`
  and `reopened` (back to `detected`) from any terminal state.**
  `update_finding_status()` on the store, `POST
  /findings/{run_id}/{finding_id}/status` on the API, `AgentLoopClient.
  update_finding_status()`, the CLI's `update-finding-status` /
  `remote-update-finding-status`, and a transition control on the dashboard's
  Optimization Queue page all expose the same state machine. Transitions are
  project-scoped and distinguish a missing finding (404) from an invalid
  transition (409). Re-diagnosing a run now **upserts** findings instead of
  deleting and recreating them: an unchanged finding keeps its reviewed
  status, and a finding that disappears from a new diagnosis is marked
  `superseded` (not silently reset to `detected`) unless it was already
  `resolved` or `dismissed`, in which case that decision is preserved. A
  `superseded` finding that reappears in a later diagnosis becomes `detected`
  again. **Compatibility:** `optimization_queue()` and the paginated finding
  listing now exclude `dismissed` and `superseded` findings in addition to
  `resolved` ones.

### Fixed

- **Trace ingestion is now idempotent and atomic on both storage backends.**
  Re-saving the same `(project_id, run_id)` — e.g. on a client retry after a
  lost response — used to append a new `usage_events` row every time, inflating
  run counts, token totals, and modeled cost. `record_usage()` now upserts on a
  new unique `(project_id, run_id)` index (existing duplicate rows are
  de-duplicated, keeping the most recent, by the migration that adds the
  constraint), and `save_trace()` upserts the trace, usage, and findings in a
  single transaction per backend, so a mid-save failure can no longer leave a
  trace without its expected usage or findings. Saving a run ID already owned
  by another project still raises the existing conflict error.

## [0.4.0] - 2026-07-18

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
- Docker deployment CI that starts the exact built image with Postgres, waits for
  API and dashboard readiness, verifies a trace round-trip, and checks the
  non-root runtime filesystem posture.

### Changed

- Selected `agentloop-profiler` as the installable distribution name while
  retaining `agentloop` as the Python import package and command-line program.
  Install published releases with `pip install agentloop-profiler`.
- Issue forms and generated optimization drafts now use only labels that exist
  in the repository, and the launch checklist records the audited/target branch,
  tag, security, Dependabot, and Actions controls for periodic owner review.
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
- Aligned the official container with the tested Python 3.13 runtime and made
  application files read-only to its non-root user; `/data` remains writable.

### Fixed

- **OpenAI instrumentation is now idempotent and stream-aware.** Wrapping the
  same client or callable more than once is a no-op, so one request records one
  event instead of doubling metrics. Streaming responses (`stream=True`) are
  finalized when the stream is consumed, closed, fails, or is cancelled — not when
  the iterator is created — so the recorded duration covers consumption and final
  usage is captured when the SDK provides it; a mid-stream error is recorded once
  and propagates unchanged. Final usage on the Responses streaming API is read from
  the terminal event's nested `response.usage`, not just a top-level `usage`. Trace
  ownership is captured at invocation time, so a stream is always recorded into the
  trace that was active when the call was made (or not recorded at all if none was),
  never into a later or unrelated trace that happens to consume it. Calls made
  without an active trace are no longer recorded and no longer raise, so they cannot
  break a successful application call.
- **The OpenAI Agents tracing processor now isolates and releases state per
  trace.** Spans are grouped by their owning trace id and released when that trace
  ends, so interleaved traces export only their own spans and processor memory no
  longer grows with the number of completed traces. A span that arrives without a
  readable trace id is attributed to the single most recently started open trace
  (or dropped if none is open) instead of being copied into every trace that ends.
  Duplicate `on_trace_end`, a missing `on_trace_start`, `shutdown`, and
  `force_flush` now have defined behavior, and `AgentLoopTracingProcessor` gained a
  `retain_exported` option to avoid holding completed `exported_traces` in memory.
  Per-trace state is released even if building or exporting the trace fails, so a
  failed export cannot re-introduce the completed-trace memory leak.
- **Trace finalization side effects now run independently.** Export, local
  storage, and upload each have their own error boundary and run in a
  deterministic order, so a failure in one no longer prevents the others when
  `fail_silently=True`. Each entry in `result["errors"]` now identifies its
  `destination` (previously a flat list of messages). A finalization in which
  every attempted destination succeeds clears the process-global
  `get_last_error()` so monitoring no longer reports a recovered failure. With
  `fail_silently=False`, the first failing destination raises the new
  `FinalizationError`, whose `result` attribute preserves already-completed
  destinations. `init()` gained a `CLEAR` sentinel to explicitly reset optional
  values such as `api_key` and `export_dir` (passing `None` still means "keep the
  current value").
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
- Docker Compose now supplies Postgres credentials through a file-backed secret
  and separate libpq parameters, so reserved password characters no longer
  corrupt a generated database URL. Explicit `AGENTLOOP_DATABASE_URL` and
  `DATABASE_URL` values remain higher-priority connection overrides.

### Security

- Enforced pull-request and required-check protection on `main`, protected
  release tags from deletion or movement, enabled private vulnerability
  reporting and Dependabot security updates, and required full-commit-SHA pins
  for every external GitHub Action and reusable workflow.
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

[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/dipeshbabu/agentloop/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.4.0
