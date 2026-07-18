# Releasing AgentLoop

This guide is for maintainers publishing official artifacts. Contributors do not
need release credentials.

## Distribution ownership and trusted publishing

The maintainer controls the existing `agentloop` project on PyPI and has selected
it as this repository's official distribution. The distribution, Python import
package, and command-line program therefore all remain `agentloop`.

The GitHub `pypi` environment requires maintainer approval and accepts only `v*`
tags. PyPI must trust owner `dipeshbabu`, repository `agentloop`, workflow
`release.yml`, and environment `pypi` before the first tag is created.

The release workflow requires the repository variable `PYPI_PUBLISH_ENABLED` to
equal `true` before its publish job will run. Leave that variable unset until the
trusted publisher is configured and verified.

## Prepare a release

1. Update the version in both `pyproject.toml` and `agentloop/version.py`.
2. Move the relevant entries in `CHANGELOG.md` from `Unreleased` into a versioned
   section with the release date.
3. Sync and run all checks from a clean worktree:

   ```bash
   uv sync --locked --all-extras --dev
   uv run --frozen pre-commit run --all-files
   uv run --frozen --all-extras python -m pytest -q
   uv build
   uv run --isolated --no-project --with twine twine check dist/*
   ```

4. Review the wheel and source archive. Confirm that the license, README, package
   code, and required data files are present and that no local traces or secrets
   are included.
5. Re-run the dependency license inventory documented in
   `THIRD_PARTY_LICENSES.md`. Review new direct and transitive terms, and inspect
   the final container separately because it also includes operating-system
   packages.
6. Merge the release preparation through the normal pull request process.

## Publish

After the PyPI trusted publisher is configured:

1. Set the repository variable `PYPI_PUBLISH_ENABLED` to `true`.
2. Create an annotated tag that exactly matches the package version:

   ```bash
   git tag -a vX.Y.Z -m "AgentLoop X.Y.Z"
   git push origin vX.Y.Z
   ```

3. Watch the `Release` workflow. Before publishing, it rejects a tag whose
   version does not match the package or whose commit is not reachable from
   `main`. It then calls the same CI workflow used for pull requests against the
   exact tagged commit: supported-Python tests, lock/pre-commit/Bandit checks,
   CLI smoke, package metadata and wheel-install smoke, and the production
   container deployment smoke must all pass.
4. The reusable CI package job builds the wheel and source archive once, checks
   those files, and uploads them as the `python-package` artifact. The publish
   job downloads those exact bytes; it never rebuilds a release artifact.
5. Verify the installed artifact in a fresh environment:

   ```bash
   python -m pip install agentloop==X.Y.Z
   python -m pip show agentloop
   agentloop --help
   ```
6. Publish GitHub release notes based on the changelog and link the workflow run.

There is currently no side-branch tag exception. If the project later adopts a
hotfix process, document and protect it before weakening the reachability gate.

Never reuse or move a published version tag. If a release is broken, fix forward
with a new patch version. Yank an artifact only when leaving it available would
harm users, and explain the reason in the changelog and release notes.
