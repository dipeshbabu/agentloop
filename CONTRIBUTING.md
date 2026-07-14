# Contributing to AgentLoop

Thanks for helping improve AgentLoop. Contributions can be code, tests,
documentation, bug reports, examples, or design feedback.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
For help using AgentLoop, see [Support](SUPPORT.md). Please report security
problems through the private process in [Security](SECURITY.md), not in a public
issue.

## Ways to contribute

- Confirm and improve an existing bug report.
- Add a regression test for a known failure.
- Improve an example, integration, error message, or operating guide.
- Propose an optimization rule with evidence and replay acceptance criteria.
- Review a pull request or help answer a reproducible usage question.

Issues labeled `good first issue` should be small and independently reviewable.
Issues labeled `help wanted` are ready for community input but may require more
project context. An assignment is not required, but leave a comment before
starting larger work so contributors do not duplicate effort.

## Before you start

- Search existing issues and pull requests before opening a new one.
- For a substantial feature, public API change, new dependency, or storage
  migration, open an issue first. Agreeing on the shape of the change saves
  work for everyone.
- Keep bug reports reproducible. Use synthetic traces and remove API keys,
  prompts, customer data, and other sensitive information.
- Small fixes and documentation improvements can go straight to a pull request.

## Set up a development environment

AgentLoop supports Python 3.10 and newer and uses
[`uv`](https://docs.astral.sh/uv/) for dependency management and task execution.
Install `uv`, then run:

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --all-extras --dev
```

`uv` creates the local environment and installs the Python version selected in
`.python-version` when needed. Install the Git hooks after the first sync:

```bash
uv run --frozen pre-commit install
```

## Understand the project layout

- `agentloop/tracer.py` and `agentloop/events.py` capture execution data.
- `agentloop/graph.py`, `agentloop/findings.py`, and `agentloop/optimizer.py`
  turn traces into execution graphs and optimization recommendations.
- `agentloop/replay.py` and `agentloop/quality.py` prove performance and quality
  changes against explicit gates.
- `agentloop/store.py`, `agentloop/server.py`, and `agentloop/client.py` provide
  persistence and hosted API boundaries.
- `agentloop/integrations/` contains optional framework and SDK adapters.
- `agentloop/cli.py` is the command-line surface; `dashboard/` is the Streamlit
  surface.
- `tests/` contains offline unit and integration-style tests. `examples/` contains
  synthetic, runnable demonstrations.

Keep dependencies pointing inward toward the core data model. Core tracing,
analysis, replay, and export code must not require a framework SDK, FastAPI,
Streamlit, or a network connection. Integrations should adapt third-party objects
at their boundary rather than leaking those types across the package.

## Make a change

1. Create a focused branch from `main`.
2. Add or update tests with the implementation.
3. Update user-facing documentation and `CHANGELOG.md` when behavior changes.
4. Run the relevant checks locally.
5. Open a pull request using the repository template.

The core tracing and analysis code should remain usable without framework SDKs.
Integrations must keep their third-party imports optional and should work with
small protocol-compatible test doubles. Prefer clear, typed Python and avoid
adding a dependency when the standard library is enough. Python follows PEP 8,
as enforced and formatted by Ruff; line length is configured to 100 characters.

### Integration changes

An integration should:

- import its third-party SDK only when that SDK is actually used;
- preserve wrapped return values, exceptions, sync/async behavior, and method
  signatures as far as the SDK permits;
- avoid recording raw secrets or full payloads by default;
- degrade gracefully when optional usage metadata is absent;
- use a protocol-compatible fake in tests instead of a live service; and
- add or update a copyable example in `docs/INTEGRATIONS.md`.

### Trace, API, and storage changes

Treat exported trace JSON, CLI output consumed by automation, HTTP response
shapes, environment variables, and database records as compatibility surfaces.
When changing one of them:

- keep existing serialized traces readable when practical;
- document additions, removals, and changed defaults;
- include migration or fallback behavior for stored data;
- add round-trip or API regression tests; and
- call out the compatibility impact in the pull request and changelog.

### Dependencies and the lockfile

Declare runtime dependencies in `[project].dependencies` or the narrowest
appropriate optional extra. Development-only tools belong in
`[dependency-groups].dev`. Explain why a new dependency is needed, review its
maintenance and license, and avoid overlapping libraries for the same job.
Update [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) when the direct license
inventory or a material transitive licensing obligation changes.

After changing dependency declarations, update and review the lockfile:

    uv lock
    uv sync --locked --all-extras --dev

Do not bundle unrelated lockfile upgrades into a feature or bug-fix pull request.
Dependabot handles routine uv and pre-commit updates.

## Run the checks

Run the same main checks as CI:

```bash
uv run --frozen pre-commit run --all-files
uv run --frozen --all-extras python -m pytest -q
uv build
```

The pre-commit hook runs repository hygiene, metadata and link validation, Ruff
linting and formatting, and a Bandit security scan. The pre-push hook also runs
the test suite. You can run Ruff directly with `uv run ruff check --fix .` and
`uv run ruff format .`.

During development, a focused test is usually faster:

```bash
uv run --frozen --all-extras python -m pytest tests/test_replay.py -q
```

If your change affects packaging or deployment, also build the container:

```bash
docker build .
```

Tests must not depend on paid APIs, network access, wall-clock timing, or real
credentials. Use the fakes in `examples/` and the patterns in `tests/`.

Add a regression test that fails before a bug fix and passes afterward. Test
public behavior rather than implementation details where possible. Changes to
time, cost, or ordering logic should cover boundary values and deterministic
ordering. Changes to persistence should cover both SQLite and shared store
contracts when applicable.

## Pull request expectations

A pull request should:

- explain the problem and the chosen approach;
- link the issue it addresses, when one exists;
- stay narrow enough to review and revert safely;
- include tests for changed behavior and regression tests for bug fixes;
- call out compatibility, schema, configuration, or migration effects;
- include before-and-after output for dashboard or report changes; and
- pass lint, tests, package build, and any applicable AgentLoop replay gates.

Maintainers may ask for a change to be split when unrelated work is bundled
together. Reviews focus on correctness, security, maintainability, compatibility,
and whether the change fits the project's direction.

Draft pull requests are welcome for early technical feedback, but they should
state what remains unresolved. A pull request is ready for final review when its
description and checklist are complete, CI is green, review conversations are
resolved, and the branch contains no unrelated formatting or generated files.

Approval does not guarantee an immediate merge. Maintainers may wait for another
review on security-sensitive or compatibility-sensitive work. Maintainers may
close a proposal that no longer fits the project, but should explain the reason
and preserve useful technical discussion.

## Changelog and compatibility policy

Add an `Unreleased` entry for user-visible additions, fixes, deprecations,
security hardening, or behavior changes. Pure refactors, tests, and typo fixes
usually do not need an entry. Use the `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, or `Security` headings from Keep a Changelog.

Before 1.0, breaking changes are possible but should still be intentional,
documented, and accompanied by a practical migration path. Deprecate a public
surface before removing it when the maintenance cost and security impact allow.

## AI-assisted contributions

AI tools are welcome as part of a contributor's workflow, but the contributor is
responsible for every submitted line. Review generated changes, verify licenses
and provenance, remove fabricated or sensitive content, and run the full relevant
test suite. Disclose substantial AI assistance in the pull request so reviewers
have the right context.

## Documentation

Use short examples that can be copied and run. Keep public documentation focused
on behavior that exists in the same pull request. If a command, environment
variable, endpoint, or return shape changes, update the README and the relevant
file under `docs/`.

## Licensing

Unless explicitly stated otherwise, any contribution intentionally submitted for
inclusion in AgentLoop is licensed under the [Apache License 2.0](LICENSE), as
described in section 5 of that license. Contributors retain copyright in their
work. The project does not currently require a separate contributor license
agreement.
