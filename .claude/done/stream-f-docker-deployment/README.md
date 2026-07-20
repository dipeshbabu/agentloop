# Stream F — Docker & deployment

> **Status: ✅ Done.** Both issues closed and shipped in `v0.5.0`. Kept here as the
> historical plan — read the current `Dockerfile`/`docker-compose.yml`/`ci.yml` for what
> was actually built, not this document, if you're touching deployment now.

## Scope

The container image, the Compose stack, and their CI coverage. Work lands in **`Dockerfile`**,
**`docker-compose.yml`**, **`.env.example`**, the docker job in
**`.github/workflows/ci.yml`**, `scripts/smoke_api.py`, and `docs/PRODUCTION.md`.

Issues: **#23** (safe Compose DB URLs for reserved password chars), **#24** (test the
production container on the Python version it ships).

## Approach for the stream as a whole

Independent of each other — either order. **#23** is the smaller, self-contained fix; **#24**
is a CI-plus-Dockerfile change that actually runs the built image. Do #23 first for a quick win.

## Stream-specific rules

- **Env vars and the container are compatibility surfaces.** Users who supply a complete
  `AGENTLOOP_DATABASE_URL` directly must keep working (#23). `.python-version`, classifiers,
  and support docs must not contradict the image's Python (#24).
- **Never log secrets** — no rendered DSN, health output, or test log may reveal the DB
  password.
- Keep the base image and uv version **pinned and reviewable** through the existing Dependabot
  process.
- Reuse `scripts/smoke_api.py` for #24 instead of writing a new smoke path.

## Cross-stream coordination

- #24 edits `.github/workflows/ci.yml`; **#25 (Stream G)** edits `release.yml`. Different
  files but both CI — review them together and make sure the container check #24 adds is the
  same one #25 wants to require before publish.
- #23's Postgres URL handling should stay consistent with how Stream A's store parses
  `AGENTLOOP_DATABASE_URL`.

## Definition of done for the stream

Both issues' acceptance criteria met; CI starts the built image and hits `/readyz` + a
trace round-trip; a punctuation-heavy DB password works end-to-end; `.env.example` and
`docs/PRODUCTION.md` updated; no secret ever logged.

See [`SHARED_CONVENTIONS.md`](../../SHARED_CONVENTIONS.md).
