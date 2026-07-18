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

## Prepare a release

### Automated (recommended)

1. From the **Actions** tab, run the **Bump version** workflow (or
   `gh workflow run bump-version.yml -f bump_type=minor`). Choose `patch`,
   `minor`, or `major`, or set an explicit `version` input instead.
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
   `patch`) before it becomes a tag. Full CI already ran in step 2; GitHub does
   not start a separate `pull_request`-triggered run for a PR opened by
   `GITHUB_TOKEN` (an anti-recursion restriction, not a skipped check).
4. Review the wheel and source archive from that CI run. Confirm that the
   license, README, package code, and required data files are present and that
   no local traces or secrets are included.
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
   `--set X.Y.Z`) locally — it does steps 1–2 below in one command, including
   the `uv.lock` refresh.
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
3. The reusable CI package job builds the wheel and source archive once, checks
   those files, and uploads them as the `python-package` artifact. The publish
   job downloads those exact bytes; it never rebuilds a release artifact.
4. The `pypi` GitHub Environment requires a maintainer to approve the
   deployment before `publish-pypi` runs — nothing publishes without that
   manual approval, regardless of how the tag was created.
5. Verify the installed artifact in a fresh environment:

   ```bash
   python -m pip install agentloop-profiler==X.Y.Z
   python -m pip show agentloop-profiler
   agentloop --help
   ```
6. Publish GitHub release notes based on the changelog and link the workflow run.

There is currently no side-branch tag exception. If the project later adopts a
hotfix process, document and protect it before weakening the reachability gate.

Never reuse or move a published version tag. If a release is broken, fix forward
with a new patch version. Yank an artifact only when leaving it available would
harm users, and explain the reason in the changelog and release notes.
