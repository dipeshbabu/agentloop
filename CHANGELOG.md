# Changelog

Changes to AgentLoop follow [Semantic Versioning](https://semver.org/).
The published version sequence starts at `0.1.0`; routine releases increment the
patch number, such as `0.1.1` and `0.1.2`. Package versions are separate from
trace, study, receipt, API and database schema versions.

## [Unreleased]

### Added

- `agentloop.optimization_search` runs a bounded, seeded search over candidate
  interventions using a host-supplied `evaluate` callback. It measures the
  baseline, then each single intervention, then greedily combines compatible
  ones while reporting their interaction. It finishes with one held-out
  evaluation. Budgets cover attempts, elapsed time and measured cost, and it
  stops rather than guess at unknown cost. Cancellation and interruption stop
  it too. Every attempt is retained; the report includes the Pareto set, net
  gain and search overhead, and a decision for review. Nothing is deployed. See
  [Optimization search](docs/OPTIMIZATION_SEARCH.md)
  ([#402](https://github.com/dipeshbabu/agentloop/issues/402)).
- `agentloop.retry_passes` labels each retry group's cause (transport, schema,
  semantic, tool or unknown) and keeps transport and semantic retries as
  legitimate. It flags retries of tools not declared idempotent as
  `unsafe_retry`, and finds repeated identical calls and oscillation.
  `propose_limits` proposes bounded `LoopLimits` only where there is evidence of
  waste, and never tighter than a completed run needed. `shadow_policy` returns
  a shadow-mode harness config, and `compare_runs` rejects a faster candidate
  that completes less often. See [Retry and loop passes](docs/RETRY_PASSES.md)
  ([#401](https://github.com/dipeshbabu/agentloop/issues/401)).
- `agentloop.schedule_compiler` compiles groups of sibling tool calls from a
  recorded run into the existing tool scheduler's plan. It uses only declared
  dependencies, effects and state reads and writes. Groups with unknown or
  unsupported evidence abstain, and timing alone never implies independence.
  Candidate groups report the scheduler's effective dependencies, resource
  conflicts, and a simulated makespan under `max_concurrency`. `tool_calls`
  binds a group to callables for `ToolScheduler.execute`. See
  [Compiling a schedule](docs/SCHEDULE_COMPILER.md)
  ([#400](https://github.com/dipeshbabu/agentloop/issues/400)).
- `agentloop.request_reuse`: request-local reuse of tool results declared
  `read` or `none`, inside a `request_scope`. A key covers the tool, its version,
  the auth scope, an optional data version and the canonical input, and a hit
  compares the stored input in full. Nothing crosses requests or tenants, and
  inputs that aren't plain JSON bypass the cache. Results are deep copied, and
  errors aren't cached. Model output reuse is a separate opt-in.
  `deduplicate_context` never removes protected segments, and `compare_reuse`
  compares measured runs while keeping failed correctness and unknown costs.
  See [Request-local reuse](docs/REQUEST_REUSE.md)
  ([#399](https://github.com/dipeshbabu/agentloop/issues/399)).
- `agentloop.pilot_records` validates first-use pilot records, which carry an
  opaque participant ID and a consent reference and refuse personal fields,
  and summarizes them with denominators. It also compares optimization methods
  with a no-optimization baseline on held-out workloads, reporting quality
  regressions, unknown costs and improved negative controls. This is part of
  [#406](https://github.com/dipeshbabu/agentloop/issues/406). No participant data
  is included.
- `agentloop.transformations`, a registry of executable transformation passes.
  Each pass declares what it applies to, the evidence it needs, the effects it
  tolerates, its objective, validation and rollback. `propose` is a dry run that
  returns a unified diff and a deterministic receipt, or abstains with reasons.
  `execute_candidate(..., allow_execution=True)` applies a candidate in a
  temporary git worktree of `HEAD`, runs a host-supplied validation command
  without a shell, removes the worktree, and checks that the original checkout
  is unchanged. Two passes ship: `config.json_set` and
  `ast.memoize_pure_function`, which needs an exactly resolved source receipt
  and a declared pure, deterministic operation. See
  [Transformation registry](docs/TRANSFORMATIONS.md)
  ([#398](https://github.com/dipeshbabu/agentloop/issues/398)).
- `agentloop.timing_sensitivity` simulates, on a recorded run, how much sooner
  it would have finished if one operation were removed or made faster. It
  reports critical-path slack, the simulated gain for each leaf operation, and
  where the bottleneck moves. It abstains on missing or inconsistent timing,
  unsupported IR evidence, or a model that can't reproduce the observed
  runtime. `compare_prediction` sets a simulated gain against measured baseline
  and candidate runtimes, with a reproducible bootstrap interval, and labels the
  two kinds of evidence separately. See
  [Timing sensitivity](docs/TIMING_SENSITIVITY.md)
  ([#397](https://github.com/dipeshbabu/agentloop/issues/397)).
- A versioned execution IR (`agentloop.ir/v1`, `agentloop.execution_ir`) over
  the execution graph. It covers typed operations with effect, idempotency,
  determinism, state reads and writes, and authorization and freshness scopes.
  Each property records whether it was declared, observed, inferred or unknown.
  Edges record their kind: containment, data, state or sequence. Write
  conflicts, cycles and malformed declarations are reported under
  `unsupported`. `independence()` never treats inferred ordering or unknown
  effects as proof that work can be reordered. Serialization is canonical, and
  legacy report graphs migrate on load. See
  [Execution IR](docs/EXECUTION_IR.md)
  ([#396](https://github.com/dipeshbabu/agentloop/issues/396)).
- Opt-in source provenance for traced functions. `traceable(capture_source=True)`,
  or `AGENTLOOP_CAPTURE_SOURCE=1`, records each span's relative path, qualified
  name, line range and file digest under `agentloop.source`. It records no source
  text and no absolute paths. Patch plans resolve these receipts against the
  checkout: only an exact match is `rewrite_eligible`, and moved, stale,
  ambiguous, missing or unverified locations are reported explicitly. Name
  matches remain as leads, and traces without receipts plan as before. See
  [Source provenance](docs/SOURCE_PROVENANCE.md)
  ([#395](https://github.com/dipeshbabu/agentloop/issues/395)).

### Changed

- API key generation, hashing and verification moved from `agentloop/store.py`
  to `agentloop/store_keys.py`, the first step in splitting the store
  ([#407](https://github.com/dipeshbabu/agentloop/issues/407)).
  `agentloop.store` still exports every public name, as the same objects, and
  `tests/test_store_public_surface.py` pins that contract.
- The finding benchmark guide now reports the archived baseline (source revision
  `41e0cc5`, precision 8 / 10) and the current rules (8 / 8) separately, with
  their counts and denominators. Before, the guide described the corrected rules
  but used the present tense for the baseline's 0.5 per-rule precision.
  `tests/test_finding_benchmark_docs.py` regenerates both tables from the
  checked-in baseline and a fresh evaluation, and fails if the guide drifts
  ([#408](https://github.com/dipeshbabu/agentloop/issues/408)).

## [0.2.10] - 2026-11-25

### Added

- `release-npm.yml` creates a "TypeScript SDK X.Y.Z" GitHub release for each
  published SDK version, with the exact tarball npm serves and its SHA-256.
  Until now only 0.1.0 had a GitHub release; 0.1.1 to 0.1.3 were published to
  npm and tagged, but not released on GitHub.
- A redesigned dashboard: light and dark themes in the AgentLoop brand colours
  (`.streamlit/config.toml`, copied into the container image), the project
  logo, sidebar navigation with icons, page headers with a one-line
  description, metric cards, readable table columns, Overview charts of runtime
  over time and runs by agent, an event waterfall for each trace, severity and
  readiness badges in the Optimization Queue, aligned cards on the Ingest and
  API Keys pages, and a centred sign-in screen for shared dashboards.
- A reusable `application-performance-comment.yml` workflow posts the
  comparison report on the pull request and updates the same comment on later
  pushes. Callers opt in with a separate job that alone receives
  `pull-requests: write`; forked and Dependabot pull requests are skipped. This
  repository's smoke workflow uses it on every pull request.
- `scripts/benchmark_scale.py` fills a fresh store and times every store query,
  API route, upload and dashboard page. CI runs it with 2,000 traces against
  SQLite and Postgres and fails on errors, operations over 15 seconds, or API
  responses over 5 MB.
- `scripts/load_test.py` runs a real multi-worker server under concurrent
  uploads, reads and a retention purge, and fails on any failed request, lost
  upload or failed purge. CI runs it against SQLite and Postgres.

### Changed

- Documentation fixes from a full pass over every page: the home page is one
  centred column instead of running against the left edge, the search field
  no longer shows the query twice, code copy buttons are centred on the first
  line, the mobile menu drops its divider lines, and the not-found page has a
  title, an explanation and links to the usual starting points.
- The documentation site and the dashboard use the exact colours of the
  AgentLoop logo: violet `#7038c9` / `#c7a1ff` for accents and links, coral
  `#d96559` / `#ff9c8f` for the active tab and second chart series, and the
  logo's ink `#30263d` / `#f5efff` for text. A test pins them to the logo files.
- The documentation site shows previous and next pages under each page's
  content: a tinted tray with "Previous" on the left and a white panel holding
  the next page's title, a one-line summary and "Next". Code blocks have an
  always-visible copy button, and the home page's workflow diagram is smaller.
- The dashboard's Optimization Queue reuses its queue for 30 seconds instead of
  rebuilding it on every click, and rebuilds it after changes made from the
  dashboard or with the new Refresh button. Re-sorting the queue at 2,000
  traces went from 1.5 s to 0.6 s.
- SQLite stores now use WAL mode, so reads no longer wait for each upload's
  commit. Under four API workers with concurrent uploads and reads, about 80%
  more reads completed in the same time. Store connections are now closed
  after each operation instead of when garbage collected, and a trace's report
  and diagnosis are built before the write transaction opens.
- Optimization queue groups list only their 25 newest members in full
  (`ranking.members`, `ranking.estimate_provenance`), with new
  `ranking.member_count` and `ranking.members_truncated` fields. Savings, impact
  and readiness still use every member. At 2,000 traces the queue response fell
  from 13 MB to 476 KB.
- The dashboard Overview and trace pickers show the newest 200 runs, with an
  "older run ID" box for the rest, and the Optimization Queue page no longer
  loads every finding.
- Trace uploads through the API are about 4.7 times faster (82 ms to 17 ms each
  locally). The API reuses one store per configuration and checks migrations
  once rather than on every call, a trace's report is built once per save, and
  an API key that has passed scrypt verification is not re-hashed on later
  requests while its stored hash exists, so revoking a key still takes effect
  immediately.

## [0.2.9] - 2026-11-24

### Fixed

- The dashboard in the published container image failed on every page with
  `PermissionError: 'runs'`: it created its demo-trace directory in the
  read-only `/app` working directory. It now uses
  `AGENTLOOP_DASHBOARD_RUNS_DIR` (set to `/data/runs` in the image) and falls
  back to a temporary directory when `./runs` cannot be created. CI renders
  every dashboard page inside the deployment container, since Streamlit's
  health endpoint stays healthy when the app script fails.

### Added

- The TypeScript SDK client covers the newer API routes: `listTraces({ label })`,
  `deleteTrace`, `listCiRuns`, `compareCiRun`, and with the admin key
  `listApiKeys`, `revokeApiKey` and `listAuditEvents`. The SDK's interop check
  exercises them against a real API server. They ship in the next npm release.
- Kubernetes manifests in `deploy/kubernetes/` for `kubectl apply -k`: the API
  (two replicas), the dashboard with project sign-in, a daily retention CronJob
  and an evaluation Postgres, with settings and secrets generated from local
  `config.env` and `secrets.env` files and an example HTTPS Ingress. Containers
  run as a non-root user with a read-only root filesystem and no capabilities.
  A NetworkPolicy lets only AgentLoop's pods reach Postgres. CI deploys them to
  a kind cluster and checks the API, every dashboard page, the hardening, the
  retention job and that other pods cannot reach Postgres. See
  `docs/KUBERNETES.md`.
- CI runs can be compared in one step. A dashboard **CI Runs** page groups
  uploaded traces by their `workflow_run` label and compares a run's newest
  baseline and candidate with the Replay Proof gates and PR comment.
  `GET /v1/ci-runs`, `GET /v1/ci-runs/{workflow_run}/comparison`,
  `AgentLoopClient.list_ci_runs` and `compare_ci_run` do the same with a
  project key; runs missing either trace return a clear 404.
- The API sends `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`
  and `Referrer-Policy: no-referrer` on every response, including 413 and CORS
  preflight responses, without a proxy. `AGENTLOOP_API_DOCS=false` stops serving
  `/docs`, `/redoc` and `/openapi.json`; the cloud stack sets it, CI checks both,
  and `production-check` warns while the docs are public.

## [0.2.8] - 2026-11-23

### Changed

- `agentloop production-check` checks shared-server settings: non-HTTPS CORS
  origins fail, an invalid request-size limit or retention period fails, and a
  disabled request limit, missing retention, dashboards without project sign-in
  and a shared `AGENTLOOP_API_KEY` warn. CI runs it inside the cloud stack.
  `doctor` and `production-check` now state the actual minimum, Python 3.11.
- The API refuses request bodies larger than `AGENTLOOP_MAX_REQUEST_BYTES`
  (default 16 MiB, `0` disables the limit) with `413`, before reading them into
  memory. Bodies without a declared length are counted as they arrive. Earlier
  versions accepted any size unless a proxy limited it. The cloud stack passes
  the setting through so it can be raised together with the proxy limit.

### Added

- An audit log of access and deletion actions. Migration 6 adds an
  `audit_events` table recording API keys created and revoked, traces deleted
  and retention purges, with the project, target, counts and acting identity
  (admin API, project key prefix, CLI, dashboard or retention schedule), written
  in the same transaction as the action. Admins read it with
  `GET /v1/audit-events`, `AgentLoopClient.list_audit_events`,
  `agentloop audit-log` or `agentloop remote-audit-log`. Key values are never
  recorded. The dashboard's Audit Log page shows the current project's events,
  including to signed-in project users.
- Trace labels are visible and searchable. Paginated trace listings include
  each trace's `metadata.labels`, and `GET /v1/traces?label=KEY=VALUE`,
  `AgentLoopClient.list_traces(label=...)`, `agentloop list-stored-traces --label`
  and a dashboard Traces filter return only traces with that label, in SQLite
  and Postgres.
- CI can send traces to an AgentLoop server. `agentloop upload` accepts
  several `--path` values and directories, validates every file before sending,
  and adds `--label KEY=VALUE` pairs under `metadata.labels`. The reusable
  `application-performance.yml` workflow takes an optional `agentloop_api_url`
  input and `agentloop_api_key` secret, and uploads the compared baseline and
  candidate traces labelled with the repository, commit, ref and workflow run.
- The container image is published for `linux/arm64` as well as `linux/amd64`
  under the same tag, so ARM cloud servers can run AgentLoop. Each architecture
  is built natively, checked, and combined into one multi-architecture tag; CI
  builds the ARM64 image and runs the API and dashboard from it.

## [0.2.7] - 2026-11-20

### Added

- Stored traces can be deleted, and a retention period enforced. Deleting a
  trace also removes its findings, usage records and interventions that compare
  it. Use `DELETE /v1/traces/{run_id}` with a project key (own project only),
  `AgentLoopClient.delete_trace`, `agentloop delete-trace` /
  `remote-delete-trace`, or the dashboard's confirmed Delete control.
  `agentloop purge-traces --older-than-days N` (or `AGENTLOOP_RETENTION_DAYS`)
  removes older traces, and the cloud stack's new `retention` service runs it
  daily.
- Project API keys can be listed and revoked. Admin routes
  `GET /v1/api-keys?project_id=` and `DELETE /v1/api-keys/{prefix}?project_id=`,
  `AgentLoopClient.list_api_keys` / `revoke_api_key`, and the CLI commands
  `list-api-keys`, `revoke-api-key`, `remote-list-api-keys` and
  `remote-revoke-api-key` show prefixes and usage times, never key values. A
  revoked key is rejected immediately, and project-mode dashboard sessions
  re-check their key every minute. Shared dashboards no longer offer the demo
  generators that write files on the host.
- Shared dashboards can sign users in by project. With
  `AGENTLOOP_DASHBOARD_REQUIRE_KEY=true`, the dashboard asks for a project API
  key, verifies it like the HTTP API, and shows only that project; the project
  field, API-key creation and the host-file patch plan are unavailable, and
  repeated failed sign-ins wait a minute. The cloud deployment stack enables it.
- Cloud deployment. Releases publish a container image to
  `ghcr.io/dipeshbabu/agentloop` (version and `latest` tags), built from the
  validated tag and checked to report the release version as a non-root user.
  `deploy/` adds a production Compose stack for one Linux server: Postgres, the
  API, the dashboard behind a login, and Caddy with automatic HTTPS, publishing
  only ports 80 and 443. CI runs the stack over HTTPS and checks API key use,
  HTTPS redirects, the upload limit, the dashboard login and unpublished
  internal ports. `docs/CLOUD_DEPLOY.md` covers single-server and managed
  platform deployments.
- `agentloop improve init --language node` generates a Node.js adapter
  (`agentloop_runner.mjs`) for the guided workflow. It imports an agent from a
  relative `.mjs`, `.js` or `.cjs` module, calls it as `answer(input, trace)`
  with a TypeScript SDK trace, and writes a failed trace when the agent throws.
  It says how to install the npm package when it is missing and names the cause
  when the agent returns before a traced call finishes. Without `--agent` it
  creates an offline example and a `package.json`. CI runs the complete guided
  workflow against the packed SDK on Node.js 22 and 24.
- Evidence-strength notes cover the cost objective. `improve start` and
  `improve compare` flag a noisy baseline when estimated cost of the same task
  varies between repetitions by at least 10% and more than the required
  improvement, and report baseline attempts whose cost cannot be measured
  because of unknown pricing or estimated token counts.

### Documentation

- The README has an Install section covering the PyPI package, the npm
  TypeScript SDK and standalone executables, an npm badge, and a JavaScript
  tracing example verified against the published npm package. The
  installation guide documents `npm install agentloop-profiler` instead of
  saying npm publication is pending.
- The README integrations table lists the TypeScript SDK and the Codex and
  Claude Code runners. The API, upgrade and installation guides cover the
  npm package's client, upgrades and version check.
- The TypeScript SDK README says how to install the `agentloop` CLI that
  analyzes its traces.

## [0.2.6] - 2026-11-19

### Added

- The Codex and Claude Code runners ship in the package as
  `python -m agentloop.runners.codex` and
  `python -m agentloop.runners.claude_code`, so they work from a `pip` install
  without a repository checkout. `examples/codex_exec_runner.py` and
  `examples/claude_code_runner.py` forward to them. The installed-wheel smoke
  check starts both runners.

### Documentation

- The README points to the Codex and Claude Code runners, and its standalone
  executable download examples use 0.2.6.
- npm release instructions require allowing direct `npm publish` for the
  trusted publisher and explain the `E404` that npm returns when no trusted
  publisher matches the workflow run.

## [0.2.5] - 2026-11-18

### Added

- `examples/claude_code_runner.py` runs Claude Code (`claude -p --output-format
  stream-json`) on guided tasks with only read-only tools pre-approved. It
  records one model span per API response with per-request usage including
  cache tokens, and one tool span per tool use. `docs/CLAUDE_CODE.md` explains
  the mapping and a one-command comparison.
- `agentloop improve compare` runs a whole guided comparison in one command.
  It captures the baseline with the unchanged runner (`--before-arg`), freezes
  the session, captures the changed runner alternating with the unchanged one
  as an interleaved control, and writes the verdict. It checks inputs before
  any runner executes, stops if no baseline attempt completes, and defaults to
  the aggregate decision. `improvement.compare()` exposes the same workflow.
- `improve start` and `improve compare` warn when a plan is too weak to
  separate a change from noise: fewer than five pairs, one repetition per task,
  or baseline latency that varies between repetitions by at least 10% and more
  than the required improvement. Notes are recorded in the frozen session and
  listed under "Evidence strength" in `plan.md`; they do not change decisions.

## [0.2.4] - 2026-11-18

### Fixed

- Repeated-tool findings now ask developers to investigate when only the tool
  name repeats. Matching opaque input keys and declared read-only behavior can
  support testing request-local reuse ahead of concurrency. Plans and estimates
  identify which change the evidence supports.

### Added

- Guided comparisons show trace latency and runner wall time in the CLI, JSON,
  Markdown, and HTML. Per-attempt timing identifies the clock source. Paired
  summaries show medians and coverage for completed, correct attempts. The
  latency objective uses trace measurements.
- Guided comparisons can require a trusted local Python verifier selected by
  the developer. The frozen session binds the script and timeout, and both
  sides must pass the verifier and their built-in quality checks.
- `improve start --max-runner-regression-ms` freezes an optional per-pair limit
  on runner wall-time increases. A configured gate requires measured monotonic
  timings and works with interleaved controls and either decision mode.

## [0.2.3] - 2026-11-17

### Added

- `agentloop improve start --decision aggregate` judges speed or cost across
  all pairs: the median paired improvement must meet the frozen minimum and
  more pairs must improve than regress. Every pair must still complete and
  pass its quality and non-performance gates. Reports include the counts and
  a non-gating sign-test p-value. The default per-pair decision is unchanged.
- `improve start` compares repetitions of each task and reports a
  `repetition_detour` finding when an attempt took more model steps or tool
  calls than its typical repetition and used at least 25% more input tokens
  (runtime when tokens are unrecorded). Evidence lists the extra calls and
  intermediate model steps. Interleaved controls are diagnosed the same way.

### Fixed

- The CLI no longer crashes with `UnicodeEncodeError` when its output is piped
  or redirected on Windows and a task ID, trace name or other text falls
  outside the console code page. Redirected output is written as UTF-8.
- `improve finish` rejects an interleaved control captured before the session
  was frozen, matching the existing candidate check.
- In aggregate decisions, report headlines count pairs that passed correctness
  and completion checks instead of implying every pair passed overall.
- The Codex example runner records one model span per model round. A message
  followed by a tool call in the same round was counted as two model calls.
- `repetition_detour` compares completed attempts only, states observed and
  typical counts instead of fractional differences, and keeps diagnosis
  counts in step with the merged findings.
- The Codex runner closes a model round when a tool item arrives only on
  completion, and measures each round from its own start.
- `improve start` accepts `per-pair` as well as `per_pair` and names the CLI
  spelling in its error.
- `docs/CODEX.md` attributes the first run's token spikes to extra model
  steps. It previously attributed them to an instruction-file read, which cost
  nothing extra when it ran in the same step as the requested read.

## [0.2.2] - 2026-11-17

### Added

- `release-npm.yml` publishes the TypeScript SDK to npm from annotated
  `typescript-vX.Y.Z` tags on main, after Node.js 22/24 build and tests, using
  trusted publishing with provenance. The SDK installs with
  `npm install agentloop-profiler`.
- `agentloop improve capture --control-arg ...` captures an unchanged control
  runner alongside the candidate, alternating which runs first on each attempt.
  `improve finish` then measures the candidate against that control, keeping
  findings frozen from the original baseline, so cache warm-up and host drift
  affect both conditions.
- `examples/codex_exec_runner.py` runs OpenAI Codex CLI (`codex exec --json`)
  on guided tasks and converts its event stream into native traces, with a
  repository task set and a documented first run in `docs/CODEX.md`.

### Changed

- Development, CI, release workflows and the Docker image use uv 0.13.7.
  The supported uv range is now 0.11.8 through 0.13, which lets Dependabot
  propose Python dependency updates again.

## [0.2.1] - 2026-11-12

### Fixed

- Published README documentation and community links use absolute destinations
  that resolve from PyPI. Repository and installed-wheel checks reject relative
  file links in the package description.
- Installation guidance uses the supported `python -m pip show agentloop-profiler`
  command to inspect the installed version.
- README HTML images and responsive sources use absolute asset URLs for PyPI.
  The shared description check validates Markdown links and HTML destinations.

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

[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.2.10...HEAD
[0.2.10]: https://github.com/dipeshbabu/agentloop/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/dipeshbabu/agentloop/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/dipeshbabu/agentloop/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/dipeshbabu/agentloop/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/dipeshbabu/agentloop/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/dipeshbabu/agentloop/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/dipeshbabu/agentloop/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/dipeshbabu/agentloop/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/dipeshbabu/agentloop/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dipeshbabu/agentloop/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dipeshbabu/agentloop/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/dipeshbabu/agentloop/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.1.0
