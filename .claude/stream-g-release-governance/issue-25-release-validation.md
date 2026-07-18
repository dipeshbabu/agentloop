# Issue #25 — Require full validation before release artifacts can be published

- **Priority:** P0 (release blocker) · **Effort:** M · **Labels:** bug, github-actions
- **Link:** https://github.com/dipeshbabu/agentloop/issues/25
- **Pair with:** #24 (Stream F) — include the container check as a prerequisite.

## Problem

`release.yml` builds and Twine-checks distribution metadata but runs **no** lint, security,
tests, CLI smoke, or container check. And `ci.yml` triggers on `main` pushes/PRs, **not** tag
pushes — so a tag can point to an unvalidated commit (side branch, stale commit, failing
extras) and still produce (and, when publishing is on, ship) an artifact.

## Key files

- `.github/workflows/release.yml:4-7` — triggers on `v*.*.*` tags.
- `.github/workflows/release.yml:27-35` — version check, `uv build`, Twine check only.
- `.github/workflows/ci.yml` — scoped to `main` pushes and PRs.

## Approach

1. **Gate publish on validating the exact tagged commit.** Options (pick one, keep a single
   auditable source of truth): a **reusable CI workflow** called by both `ci.yml` and
   `release.yml`; required-workflow-run verification; or minimal duplicated release gates.
2. **Run the full gate on the tag commit:** lock validation, Ruff/pre-commit, Bandit, full
   pytest, `uv build` + Twine check, wheel install + CLI smoke, and applicable container
   checks (from #24). Cover the documented Python support policy and required extras.
3. **Reject tags not reachable from the protected default branch** unless a documented hotfix
   process applies. Enforce tag == package version.
4. **Build once, publish those exact bytes.** Keep publish behind the `pypi` environment +
   trusted publishing.
5. **A failed prerequisite makes publish impossible** (job dependency), not merely red after
   upload. Keep permissions least-privilege and actions commit-pinned.

## Acceptance criteria (from the issue)

- [ ] The exact tag commit passes lock validation, Ruff/pre-commit, Bandit, full test suite, package build/check, wheel install/CLI smoke, and applicable container checks before publish.
- [ ] Release validation covers the documented Python support policy and required extras.
- [ ] Tags not reachable from the protected default branch are rejected unless a documented hotfix process applies.
- [ ] Tag and package versions must match.
- [ ] Publish remains protected by the `pypi` environment and trusted publishing.
- [ ] Artifacts are built once and the validated bytes are the bytes published.
- [ ] Workflow permissions remain least-privilege and actions stay commit-pinned.
- [ ] A failed prerequisite makes the publish job impossible, not merely red after upload.

## Testing

- Exercise via a dry-run tag on a branch: confirm the gate runs and that a deliberately-failing
  commit cannot reach the publish job. Confirm tag/version mismatch and non-default-branch tag
  are rejected.

## Compatibility / risk

- Depends on #26 (name) before real publishing is enabled, and on #27 (branch/tag protection)
  for the "reachable from protected default branch" check to be meaningful.
- Reuse `ci.yml` rather than duplicating gates to avoid drift.
