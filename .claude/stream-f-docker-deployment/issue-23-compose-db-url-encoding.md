# Issue #23 — Build Compose database URLs safely for reserved password characters

- **Priority:** P1 · **Effort:** S · **Labels:** bug, docker
- **Link:** https://github.com/dipeshbabu/agentloop/issues/23

## Problem

Compose interpolates the raw Postgres user/password/db name into a URI:

```yaml
AGENTLOOP_DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
```

A password like `pa/ss` produces `postgresql://agentloop:pa/ss@db:5432/agentloop` — the slash
terminates userinfo parsing, so host/path are misread. `?`, `#`, `%`, and other reserved
characters break it similarly. Secure password generators commonly emit such punctuation, so
the DB starts with the literal password while clients parse a different DSN.

## Key files

- `docker-compose.yml:21` and `:54` — API and dashboard both build the URI directly.
- `docs/PRODUCTION.md` — encourages long random secrets but doesn't mention URI encoding.

## Approach

1. **Stop building a URI from raw secret components.** Prefer separate libpq connection
   parameters / env vars (`PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGPORT`), **or** a
   small entrypoint that percent-encodes each component correctly before assembling the DSN.
2. **Keep the direct-DSN path working:** users who set a complete `AGENTLOOP_DATABASE_URL`
   themselves must be unaffected (verify precedence in the store's config parsing).
3. **Never log the secret-bearing DSN.**
4. **Docs:** `.env.example` and `docs/PRODUCTION.md` explain supported secret formats and
   rotation.
5. **Smoke coverage:** include a generated punctuation-heavy password in Compose
   validation/smoke.

## Acceptance criteria (from the issue)

- [ ] API and dashboard connect when the password contains representative URI-reserved characters.
- [ ] The same literal password reaches Postgres and psycopg.
- [ ] No rendered config, health output, or test log reveals the password.
- [ ] `.env.example` and production docs explain supported secret formats and rotation.
- [ ] Compose validation/smoke coverage includes a generated punctuation-heavy password.
- [ ] Any change remains compatible with users who supply a complete `AGENTLOOP_DATABASE_URL`.

## Testing

- A small unit test for the encoding/param-building helper across reserved chars (`/ ? # % @ :`).
- Compose smoke (can pair with #24's container job) using a password like `p@/s?s#1%2`.

## Compatibility / risk

- Changing how the DSN is assembled is an env/deployment compatibility surface — document the
  new variables and keep `AGENTLOOP_DATABASE_URL` as an override.
