# Releasing AgentLoop

This guide is for maintainers publishing official artifacts. Contributors do not
need release credentials.

## Distribution name and trusted publishing

The official PyPI distribution is `agentloop-profiler`. The Python import package
and command-line program remain `agentloop`, so users install
`agentloop-profiler` and write `import agentloop`.

The GitHub `pypi` environment requires maintainer approval and accepts only `v*`
tags. The PyPI pending publisher for project `agentloop-profiler` must trust owner
`dipeshbabu`, repository `agentloop`, workflow `release.yml`, and environment
`pypi` before the first tag is created.

The release workflow requires the repository variable `PYPI_PUBLISH_ENABLED` to
equal `true` before its publish job will run. Leave that variable unset until the
trusted publisher is configured and verified.

### One-time setup for the automated flow

The `Bump version` workflow below opens a pull request using the default
`GITHUB_TOKEN`. That requires the repository setting **Settings → Actions →
General → Workflow permissions → "Allow GitHub Actions to create and approve
pull requests"** to be enabled. Without it, the workflow's `gh pr create` step
fails with a permissions error; use the manual alternative until it's turned on.

## Versioning policy

Default to a **patch** bump (`0.5.0` → `0.5.1` → `0.5.2` → ...). Reserve
**minor** for a release whose reason to ship is a notable user-facing
feature, and **major** for a breaking change (before 1.0, any deliberate
breaking change). This is standard [SemVer](https://semver.org/) — and what
Conventional Commits-driven tools apply automatically from
`fix:`/`feat:`/`BREAKING CHANGE` — but here it's a judgment call the person
running the release makes explicitly rather than something inferred from
commit messages. Don't reach for `minor` just because a release happens to
include a new feature among other changes; reserve it for when the feature
itself is the reason to cut that release.

## Prepare a release

### Automated (recommended)

1. From the **Actions** tab, run the **Bump version** workflow (or
   `gh workflow run bump-version.yml -f bump_type=patch`). Pick the bump type
   per the [versioning policy](#versioning-policy) above (the workflow
   defaults to `patch`), or set an explicit `version` input instead.
2. The workflow validates `main` (the same checks as CI — lint, tests on both
   supported Python versions against SQLite and Postgres, package build, Docker
   deployment smoke), then runs `scripts/bump_version.py`, which:
   - moves `CHANGELOG.md`'s `## [Unreleased]` content into a new dated
     `## [x.y.z]` section (and refuses to run if that section is empty — there
     would be nothing to release);
   - updates `pyproject.toml` and `agentloop/version.py` to the same version;
     and
   - refreshes `uv.lock`.
3. It opens a `Prepare agentloop-profiler x.y.z release` pull request. Review
   the changelog placement and the version choice like any other PR — this is
   the point to catch a wrong bump type (e.g. a breaking change bumped as
   `patch`) before it becomes a tag. `main`'s branch-protection ruleset requires
   `Python 3.10`, `Python 3.13`, `Docker image and deployment smoke`,
   `Package artifact and wheel smoke`, `Replay and optimization gates`,
   `Analyze Python`, and `Review dependency changes` to pass, with no bypass —
   but those all come from `pull_request` triggers, which never fire for a PR
   opened by `GITHUB_TOKEN` (a GitHub Actions anti-recursion restriction).
   `bump-version.yml` works around that by dispatching `ci.yml`, `codeql.yml`,
   `dependency-review.yml`, and `agentloop-performance.yml` directly against the
   release branch (`workflow_dispatch` is exempt from the restriction) as its
   last step, so those checks run and report on the PR normally. Give them a
   minute to appear. If any are still missing after that — the dispatch call
   itself failed, not the check — push any commit to the branch yourself, or
   close and reopen the PR, using your own account rather than a bot token;
   either triggers the checks the ordinary way.
4. Review the wheel, source archive, and four standalone CLI artifacts from that
   CI run. Confirm that the license, README, package code, and required data
   files are present; run the executable for any locally available platform;
   and verify that no local traces or secrets are included.
5. Re-run the dependency license inventory documented in
   `THIRD_PARTY_LICENSES.md`. Review new direct and transitive terms, and
   inspect the final container separately because it also includes
   operating-system packages.
6. Merge the pull request. That's the last manual step before publishing —
   merging pushes the version bump to `main`, which triggers `tag-release.yml`
   (see [Publish](#publish)).

### Manual alternative

Use this if the one-time setup above isn't done yet, or to prepare a release
without dispatching the workflow:

1. Update the version in both `pyproject.toml` and `agentloop/version.py`, or
   run `uv run --frozen python scripts/bump_version.py patch|minor|major` (or
   `--set X.Y.Z`) locally, per the [versioning policy](#versioning-policy)
   above — it does steps 1–2 below in one command, including the `uv.lock`
   refresh.
2. Move the relevant entries in `CHANGELOG.md` from `Unreleased` into a
   versioned section with the release date.
3. Sync and run all checks from a clean worktree:

   ```bash
   uv sync --locked --all-extras --dev
   uv run --frozen pre-commit run --all-files
   uv run --frozen --all-extras python -m pytest -q
   uv build
   uv run --isolated --no-project --with twine twine check dist/*
   ```

4. Review the wheel and source archive as in step 4 above, and re-run the
   license inventory as in step 5 above.
5. Merge the release preparation through the normal pull request process.
   Merging still triggers `tag-release.yml` automatically (it watches for any
   change to `agentloop/version.py` on `main`, regardless of how the PR was
   produced) — see [Publish](#publish). To tag by hand instead, skip that
   workflow's result and push a matching annotated tag yourself:

   ```bash
   git tag -a vX.Y.Z -m "AgentLoop X.Y.Z"
   git push origin vX.Y.Z
   ```

## Publish

After the PyPI trusted publisher is configured and `PYPI_PUBLISH_ENABLED` is
`true`:

1. Once the release-prep PR merges, `tag-release.yml` reads the version from
   `agentloop/version.py` on `main`, and — unless a tag for that version
   already exists — creates and pushes the matching annotated tag, then
   dispatches `release.yml` against it. No further action is needed to get to
   step 2; this replaces the old manual `git tag` step.
2. Watch the `Release` workflow. Before publishing, its `guard` job rejects a
   tag whose version does not match the package or whose commit is not
   reachable from `main` — this can no longer actually happen for a tag
   produced by `tag-release.yml`, since it derives the tag directly from the
   commit it just verified is on `main`, but the guard still runs as a
   defense-in-depth check (including for a tag pushed by hand). `release.yml`
   then calls the same CI workflow used for pull requests against the exact
   tagged commit: supported-Python tests, lock/pre-commit/Bandit checks, CLI
   smoke, package metadata and wheel-install smoke, and the production
   container deployment smoke must all pass.
3. The reusable CI jobs build the wheel, source archive, and native standalone
   executables for Linux x86-64, Windows x86-64, macOS Intel, and macOS Apple
   silicon. Every executable must pass `--help`, generate a demo trace, and read
   that trace before it is uploaded. Publish jobs download those exact bytes;
   they never rebuild a release artifact.
4. The `pypi` GitHub Environment requires a maintainer to approve the
   deployment before `publish-pypi` runs — nothing publishes without that
   manual approval, regardless of how the tag was created.
5. Independently of the PyPI environment, `publish-github` creates the GitHub
   Release when needed and attaches the validated Python distributions, four
   standalone executables, four platform-specific complete dependency-notice
   bundles, `LICENSE`, `THIRD_PARTY_LICENSES.md`, and `SHA256SUMS`. The release
   fails closed if a dependency notice is missing. Rerunning the workflow updates
   those assets in place.
6. Verify the installed Python artifact in a fresh environment:

   ```bash
   python -m pip install agentloop-profiler==X.Y.Z
   python -m pip show agentloop-profiler
   agentloop --help
   ```
7. Download one available standalone executable from the GitHub Release, verify
   it against `SHA256SUMS`, and run `agentloop --help`. Review the generated
   release notes and add any important changelog context they missed.

There is currently no side-branch tag exception. If the project later adopts a
hotfix process, document and protect it before weakening the reachability gate.

Never reuse or move a published version tag. If a release is broken, fix forward
with a new patch version. Yank an artifact only when leaving it available would
harm users, and explain the reason in the changelog and release notes.
