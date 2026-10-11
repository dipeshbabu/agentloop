# Releasing AgentLoop

The official PyPI distribution is `agentloop-profiler`; Python imports and the
CLI remain `agentloop`. Release versions start at `0.1.0`, followed by routine
patch releases such as `0.1.1` and `0.1.2`. A version labels the package; native
trace/study/receipt schemas, API routes and database migrations have their own
versions and do not reset with the package.

## Versioning policy

Default to a **patch** bump (`0.1.0` → `0.1.1` → `0.1.2` → ...). Reserve
**minor** for a release whose reason to ship is a notable user-facing
feature, and **major** for a breaking change (before 1.0, any deliberate
breaking change). The maintainer chooses the bump explicitly.
Don't reach for `minor` just because a release happens to
include a new feature among other changes.

`scripts/bump_version.py` rejects repeated or lower versions, updates package and
runtime metadata together, moves nonempty Unreleased entries to a dated changelog
section, and refreshes `uv.lock`. The one-time initial numbering reset does not
weaken that rule for future releases.

## Prepare and review

Run **Prepare release** on `main`, or:

```bash
gh workflow run prepare-release.yml --ref main -f bump_type=patch
```

The workflow validates main before creating a preparation branch and PR. Its
version input accepts an explicit greater X.Y.Z override. Inputs pass through
quoted environment variables, and preparation never force-overwrites a branch.
Fresh CI, CodeQL, dependency, replay and application checks run against the exact
preparation head, including PRs created with GitHub's bot token.

The repository must allow Actions to create PRs for this automated flow. A manual
alternative is to run the existing version helper in a clean branch:

```bash
uv run --frozen python scripts/bump_version.py patch
uv run --frozen pre-commit run --all-files
uv run --frozen --all-extras python -m pytest -q
uv build
uv run --isolated --no-project --with twine twine check dist/*
```

Review the changelog, source/compatibility changes, dependency license inventory,
wheel/sdist and four standalone CLI artifacts. Follow [UPGRADING.md](UPGRADING.md)
for existing clients and stores. Required final-head checks must pass, actual
review findings must be resolved, and the PR must merge normally. Skipped bot
reviews are not approvals.

Merging preparation does not tag or publish. Once the final source is reviewed
and its main checks pass, explicitly create the matching annotated tag:

```bash
git switch main
git pull --ff-only origin main
git tag -a vX.Y.Z -m "AgentLoop X.Y.Z"
git push origin vX.Y.Z
```

## Validate and publish

`release.yml` checks that the ref is an annotated semantic version tag, exactly
matches package metadata and points to a commit reachable from main. It runs the
reusable full CI against that exact tag: Python 3.10/3.13/3.14, SQLite/Postgres,
lock/format/security checks, Docker deployment, installed-wheel smoke and four
standalone platform builds. Publication downloads those validated bytes instead
of rebuilding them. Concurrent publishing runs for one ref are serialized.

The PyPI trusted publisher must match owner `dipeshbabu`, repository `agentloop`,
workflow **`release.yml`**, and environment `pypi`. Preserve that filename when
editing the automation. `PYPI_PUBLISH_ENABLED=true` enables the publish job; the
protected `pypi` environment still requires its configured reviewer approval.
No API token belongs in source or workflow logs.

GitHub publication attaches the validated distributions, platform executables,
complete dependency notices, license files and `SHA256SUMS`. Notes come from the
exact version's changelog, rather than importing obsolete PR/release titles.
Recover an interrupted tagged release by explicitly dispatching:

```bash
gh workflow run release.yml --ref vX.Y.Z
```

Do not reuse a published version or move its tag. Publish a new patch for a fix.
PyPI cannot restore deleted releases or accept a previously used filename.

## Verify publication

Download and verify the release checksum manifest and one available standalone
executable, then run its `--help`. In an isolated Python environment:

```bash
python -m pip install agentloop-profiler==X.Y.Z
python -m pip show agentloop-profiler
agentloop --help
```

The wheel's adoption smoke also exercises quickstart, qualified JSON/HTML,
studies, retained quality failures and interventions:

```bash
path/to/wheel-env/bin/python -I scripts/smoke_installed_wheel.py --expected-version X.Y.Z
```

On Windows use `path/to/wheel-env/Scripts/python.exe`. A merged PR, tag, or GitHub
release does not prove PyPI publication; verify each destination independently.

For the initial numbering restart, install the exact `0.1.0` version in existing
environments and refresh pinned requirements. Removing earlier PyPI releases
requires the owner's [project management page](https://pypi.org/manage/project/agentloop-profiler/releases/).
Keep the registered project and its trusted publisher; removing the whole project
can release its name. See [PyPI's deletion and filename rules](https://pypi.org/help/#file-name-reuse).
