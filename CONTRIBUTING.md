# Contributing to AgentLoop

Thank you for helping improve AgentLoop. Contributions can be code, tests,
documentation, examples, bug reports, reviews, or design feedback.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
For usage help, see [Support](SUPPORT.md). Report security vulnerabilities
through the private process in [Security](SECURITY.md), never in a public issue.

## Start with the smallest useful path

Different changes need different amounts of coordination.

| Change | Start here | Open a pull request directly? |
| --- | --- | --- |
| Documentation, examples, tests, or a small error-message improvement | Search existing issues and pull requests | Yes, when the change is focused |
| Reproducible bug fix | Use an existing issue or open a bug report with a minimal reproduction | Usually |
| New feature, integration, optimization rule, or required dependency | Open a feature request and agree on scope first | After the direction is clear |
| Public API, trace schema, storage, migration, authentication, or deployment change | Open an issue describing compatibility and rollout | Not before design discussion |
| Security vulnerability | Follow [SECURITY.md](SECURITY.md) | Never through a public pull request first |

Issues labeled `good first issue` should be small and independently reviewable.
Issues labeled `help wanted` are ready for community input but may require more
project context. You do not need an assignment for a small issue, but leave a
comment before starting larger work so contributors do not duplicate effort.

Before sharing a trace, log, screenshot, database row, or reproduction, replace
prompts, outputs, tool arguments, URLs, identifiers, credentials, and customer
data with synthetic values.

## First-time setup

AgentLoop supports Python 3.10 and newer and uses
[`uv`](https://docs.astral.sh/uv/) for dependency management and task execution.
The repository's default development interpreter is declared in
[`.python-version`](.python-version).

### Clone the repository

External contributors should fork AgentLoop on GitHub and clone their fork:

```bash
git clone https://github.com/YOUR-USER/agentloop.git
cd agentloop
git remote add upstream https://github.com/dipeshbabu/agentloop.git
```

Contributors with write access may clone the canonical repository directly:

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
```

Before starting work from a fork, update `main` from the canonical repository and
create a focused branch:

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git switch -c docs/improve-guide
```

Use a branch name that describes the actual change. When working from the
canonical repository, update `main` from `origin` instead.

### Create the environment

Install `uv`, then create a lightweight development environment:

```bash
uv sync --locked --dev
uv run --frozen agentloop --help
```

Install all optional dashboard, server, Postgres, and instrumentation
dependencies only when your change needs them:

```bash
uv sync --locked --all-extras --dev
```

### Git hooks

A pre-commit hook catches formatting, repository-policy, link, metadata, and
security problems before a pull request. Install it with:

```bash
uv run --frozen pre-commit install --hook-type pre-commit
```

The optional pre-push hook runs the full test suite and can take longer:

```bash
uv run --frozen pre-commit install --hook-type pre-push
```

Running `pre-commit install` without a hook type installs both hooks because the
repository configures both as defaults.

## Choose and scope the change

1. Search open and closed issues and pull requests for the same problem.
2. Read the issue body and latest comments; GitHub is the live source of scope
   and status.
3. For work that changes a compatibility surface or spans several subsystems,
   describe the intended behavior, non-goals, and migration path before coding.
4. Keep one pull request focused on one problem. Avoid unrelated formatting,
   generated artifacts, dependency upgrades, or refactors.
5. Use a draft pull request when early review would prevent wasted work.

Small documentation corrections, deterministic examples, regression tests, and
narrow bug fixes can go directly to a pull request when their intent is clear.

## Project map and architecture boundaries

| Area | Main locations |
| --- | --- |
| Trace capture and runtime context | `agentloop/tracer.py`, `agentloop/events.py`, `agentloop/runtime.py`, `agentloop/decorators.py` |
| Native schema and telemetry interop | `agentloop/schema.py`, `agentloop/otel.py` |
| Execution graphs and findings | `agentloop/graph.py`, `agentloop/findings.py`, `agentloop/optimizer.py`, `agentloop/savings.py` |
| Replay, quality, CI, and value analysis | `agentloop/replay.py`, `agentloop/quality.py`, `agentloop/ci.py`, `agentloop/value.py` |
| Persistence and migrations | `agentloop/store.py`, `agentloop/migrations.py` |
| HTTP and client boundaries | `agentloop/server.py`, `agentloop/client.py` |
| Framework and SDK adapters | `agentloop/integrations/` |
| User interfaces | `agentloop/cli.py`, `dashboard/` |
| Runnable examples and tests | `examples/`, `tests/` |
| User and operator documentation | `README.md`, `docs/` |

Keep dependencies pointing inward toward the core data model. Core tracing,
analysis, replay, schema, and export code must remain usable without a framework
SDK, FastAPI, Streamlit, Postgres, or a network connection. Optional integrations
should adapt third-party objects at their boundary instead of leaking those types
through the core package.

Treat exported trace JSON, automation-facing CLI output, HTTP payloads,
environment variables, database records, and documented Python APIs as
compatibility surfaces.

## Implement the change

Create a focused branch from `main`, add or update tests with the implementation,
and update documentation and the changelog when behavior changes.

Prefer clear, typed Python and the standard library when it is sufficient.
Python follows PEP 8 as enforced and formatted by Ruff; line length is configured
to 100 characters.

### Integration changes

An integration should:

- import its third-party SDK only when the integration is used;
- remain idempotent when the same object is instrumented more than once;
- preserve return values, exceptions, cancellation, sync/async behavior,
  streaming or generator lifecycles, and method signatures as far as the SDK
  permits;
- avoid recording raw secrets or complete prompts, outputs, and tool payloads by
  default;
- degrade gracefully when optional usage or model metadata is absent;
- use protocol-compatible fakes instead of live services in tests; and
- add or update a copyable, offline example in
  [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

### Trace, API, and storage changes

When changing a compatibility surface:

- keep existing serialized traces readable when practical;
- update the versioned contract in
  [docs/TRACE_SCHEMA.md](docs/TRACE_SCHEMA.md) when trace behavior changes;
- document additions, removals, changed defaults, and error behavior;
- include migration, fallback, or deprecation behavior;
- add round-trip, API, project-isolation, or migration regression tests as
  applicable;
- cover both SQLite and Postgres store contracts when persistence behavior
  changes; and
- explain the compatibility and rollout impact in the pull request and
  changelog.

### Dependencies and the lockfile

Declare runtime dependencies in `[project].dependencies` or the narrowest
appropriate optional extra. Development-only tools belong in
`[dependency-groups].dev`.

Explain why a new dependency is necessary, review its maintenance and license,
and avoid overlapping libraries for the same job. Update
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) when the direct license
inventory or a material transitive obligation changes.

After changing dependency declarations, update and review the lockfile:

```bash
uv lock
uv sync --locked --all-extras --dev
```

Do not bundle unrelated lockfile upgrades into a feature or bug-fix pull request.
Dependabot handles routine uv, pre-commit, action, and container updates.

## Validate efficiently

Start with the smallest check that can disprove the change, then expand before
final review.

For documentation or metadata:

```bash
uv run --frozen pre-commit run --files CONTRIBUTING.md
```

For a focused Python change:

```bash
uv run --frozen --all-extras python -m pytest tests/test_replay.py -q
uv run --frozen pre-commit run --files agentloop/replay.py tests/test_replay.py
```

Replace the example paths with the files and tests relevant to your change.

| Change type | Minimum local validation before opening or updating a pull request |
| --- | --- |
| Documentation or examples | File-scoped pre-commit checks; run the example when it is executable |
| Python behavior | Relevant focused tests and file-scoped pre-commit checks |
| Framework integration | Focused integration tests plus the documented offline example |
| Trace schema, API, migration, or storage | Relevant contract tests and the full all-extras suite |
| Dashboard | Relevant parsing or AppTest coverage and the affected user flow |
| Packaging or release | Full suite, `uv build`, and installed-wheel or workflow smoke tests |
| Docker or deployment | Full suite and `docker build .` when Docker is available |

Before marking a code pull request ready for final review, run the repository's
main local checks:

```bash
uv run --frozen pre-commit run --all-files
uv run --frozen --all-extras python -m pytest -q
uv build
```

The pre-commit checks cover repository hygiene, metadata and local links, Ruff
linting and formatting, and Bandit. GitHub Actions also exercises supported
Python versions, Postgres contracts, package artifacts, standalone executables,
security checks, replay gates, and the container deployment.

You do not need to reproduce every operating system or hosted CI service locally.
State exactly what you ran, and identify unavailable or skipped validation in the
pull request rather than claiming it passed.

### Test expectations

Tests must not depend on paid APIs, network access, wall-clock timing, or real
credentials. Use deterministic synthetic data and protocol-compatible fakes.

A bug fix should include a regression test that fails before the fix and passes
after it. Test public behavior rather than implementation details where possible.
Time, cost, savings, and ordering changes should cover boundary values and
deterministic ordering. Persistence changes should cover the shared store
contract and project isolation.

## Open the pull request

Push the branch to your fork or repository:

```bash
git push -u origin docs/improve-guide
```

Open a pull request against `dipeshbabu/agentloop:main`, then use the repository
pull request template and include:

- the problem and why the chosen approach is appropriate;
- the related issue, when one exists;
- the exact commands run and their results;
- tests for changed behavior and regression coverage for bug fixes;
- public API, schema, configuration, dependency, security, storage, or migration
  effects;
- before-and-after output for dashboard, CLI, or report changes;
- documentation and an `Unreleased` changelog entry when required;
- any validation that was skipped or unavailable; and
- disclosure of substantial AI assistance.

Keep traces, logs, screenshots, and fixtures free of credentials and private
data. Maintainers may ask for unrelated changes to be split so each pull request
can be reviewed and reverted safely.

Draft pull requests are welcome. A pull request is ready for final review when
its description and checklist are complete, required CI is green, review
conversations are resolved, and the branch contains no unrelated or generated
files.

Approval does not guarantee an immediate merge. Security-sensitive,
compatibility-sensitive, or cross-cutting changes may require another review.
When a proposal no longer fits the project, maintainers should explain the
decision and preserve useful technical discussion.

## Review and automation

GitHub Actions provides the required deterministic checks. CodeRabbit performs an
automatic first-pass review when a pull request is ready and reviews new pushes
incrementally. Its repository configuration focuses on correctness, security,
reliability, compatibility, and missing regression coverage; Ruff and pre-commit
handle routine formatting and style.

CodeRabbit supplements CI and human review. It cannot approve a merge, and a
clean bot review does not replace passing checks or maintainer approval. Address
an actionable finding or reply with the technical reason it does not apply so
the decision remains visible.

Use `@coderabbitai review` to review changes since the previous pass or
`@coderabbitai full review` after a substantial rewrite. The `pause` and `resume`
commands are available for work-in-progress branches; see the
[CodeRabbit review commands](https://docs.coderabbit.ai/reference/review-commands)
for the complete command list.

## Changelog and compatibility policy

Add an `Unreleased` entry for user-visible additions, fixes, deprecations,
security hardening, or behavior changes. Pure refactors, tests, internal planning
documents, and typo-only documentation fixes usually do not need an entry. Use
the `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, or `Security` headings
from Keep a Changelog.

Before 1.0, breaking changes remain possible, but they must be intentional,
documented, and accompanied by a practical migration path. Deprecate a public
surface before removing it when maintenance cost and security impact allow.

Contributors do not bump the package version or move `Unreleased` into a dated
section. Version changes, changelog dating, release tags, and publishing are
maintainer actions described in [docs/RELEASING.md](docs/RELEASING.md).

## AI-assisted contributions

AI tools are welcome, but the contributor is responsible for every submitted
line. Review generated changes, verify licenses and provenance, remove
fabricated or sensitive content, and run the relevant tests. Disclose substantial
AI assistance in the pull request so reviewers have the right context.

## Documentation

Use short examples that can be copied and run. Include expected output or the
observable success condition when it helps a reader confirm the example worked.
Keep public documentation limited to behavior available in the same pull
request.

When a command, environment variable, endpoint, serialized field, or return shape
changes, update the README and the relevant file under `docs/`.

## Licensing

Unless explicitly stated otherwise, a contribution intentionally submitted for
inclusion in AgentLoop is licensed under the [Apache License 2.0](LICENSE), as
described in section 5 of that license. Contributors retain copyright in their
work. The project does not currently require a separate contributor license
agreement.
