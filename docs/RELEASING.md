# Releasing AgentLoop

This guide is for maintainers publishing official artifacts. Contributors do not
need release credentials.

## Current distribution-name blocker

The `agentloop` project name on PyPI is already owned by an unrelated package.
Do not create a release tag or enable PyPI publishing until the project has
chosen and reserved a unique distribution name, or has verified legitimate
ownership of the existing name. The Python import package can remain `agentloop`
even if the installable distribution uses a different name.

The release workflow requires the repository variable `PYPI_PUBLISH_ENABLED` to
equal `true` before its publish job will run. Leave that variable unset while the
name is unresolved.

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

After the distribution name and PyPI trusted publisher are configured:

1. Set the repository variable `PYPI_PUBLISH_ENABLED` to `true`.
2. Create an annotated tag that exactly matches the package version:

   ```bash
   git tag -a vX.Y.Z -m "AgentLoop X.Y.Z"
   git push origin vX.Y.Z
   ```

3. Watch the `Release` workflow. It checks the tag, builds and validates both
   distributions, and publishes through PyPI trusted publishing.
4. Verify the installed artifact in a fresh environment using the final chosen
   distribution name.
5. Publish GitHub release notes based on the changelog and link the workflow run.

Never reuse or move a published version tag. If a release is broken, fix forward
with a new patch version. Yank an artifact only when leaving it available would
harm users, and explain the reason in the changelog and release notes.
