# Issue #24 — Test the production container on the Python version it ships

- **Priority:** P1 · **Effort:** M · **Labels:** bug, github-actions, docker
- **Link:** https://github.com/dipeshbabu/agentloop/issues/24

## Problem

The image uses **Python 3.14**, but the CI matrix tests **3.10 and 3.13**, and the docker job
only runs `docker build .` — it never starts the image, imports the package, checks
non-root/data config, or hits HTTP readiness. So version/dependency/entrypoint/permission/
health failures can ship while all checks are green.

## Key files

- `Dockerfile:1` — `python:3.14-slim`.
- `.python-version` — selects 3.13.
- `.github/workflows/ci.yml:19` — matrix tests only 3.10 and 3.13.
- `.github/workflows/ci.yml:67-74` — docker job runs only `docker build .`.
- `scripts/smoke_api.py` — existing end-to-end API smoke, currently unused by the container job.

## Approach

1. **Resolve the version contradiction.** Either pin the image to the highest fully-tested
   version (3.13) **or** add 3.14 to the test matrix. Make `.python-version`, classifiers, and
   support docs agree with the choice.
2. **Actually run the built image in CI.** After `docker build`, start the container, wait for
   `/readyz`, then run `scripts/smoke_api.py` to upload and read back a synthetic trace.
3. **Verify runtime posture:** the process runs as the **non-root** user and can write only
   intended data paths.
4. **Fail the job non-zero** on any startup/readiness/smoke failure. Ensure logs/health expose
   no secrets. Keep base image + uv version pinned/reviewable.

## Acceptance criteria (from the issue)

- [ ] The Python version in the image is explicitly covered by the package test suite.
- [ ] Classifiers, support docs, `.python-version`, and container policy do not contradict one another.
- [ ] CI starts the exact built image and waits for `/readyz`.
- [ ] The smoke path uploads and reads a synthetic trace.
- [ ] The smoke verifies the process runs as the non-root user and can write only intended data paths.
- [ ] Container logs and health checks expose no secrets.
- [ ] A failed startup/readiness/smoke exits the Docker job non-zero.
- [ ] The image base and uv version remain pinned/reviewable through the existing update process.

## Testing

- The CI docker job itself is the test: build → run → `/readyz` poll → `smoke_api.py`
  round-trip → non-root/writable-path assertions.

## Compatibility / risk

- If pinning down from 3.14 to 3.13, confirm no code relies on 3.14-only behavior.
- Coordinate with **#25 (Stream G)**: the container check added here is what #25 should
  require before publishing a release.
