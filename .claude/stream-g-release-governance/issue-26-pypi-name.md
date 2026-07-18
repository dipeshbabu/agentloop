# Issue #26 — Choose and reserve a publishable PyPI distribution name

- **Priority:** P0 (release blocker) · **Effort:** S · **Labels:** documentation, enhancement
- **Link:** https://github.com/dipeshbabu/agentloop/issues/26
- **Do first in Stream G.** Involves a human decision + a PyPI reservation an agent cannot perform.

## Problem

`pyproject.toml` declares the distribution name `agentloop`, but that name is already published
on PyPI by an unrelated project. The first release cannot publish under it, and enabling
publishing would fail or confuse `pip install agentloop` users.

## Key files

- `pyproject.toml:6` — `name = "agentloop"`.
- `docs/RELEASING.md:8-10` — warns not to tag/publish until a unique name is reserved.
- `docs/OPEN_SOURCE_CHECKLIST.md:65-68` — name decision + trusted-publisher setup incomplete.

## Approach

**Owner (human) actions — cannot be automated:**
- Choose a unique final distribution name; verify availability on PyPI; **reserve** it.
- Configure a PyPI **trusted publisher** for this repo + a protected **`pypi`** environment.
- Set `PYPI_PUBLISH_ENABLED=true` only when publishing is actually ready.

**Agent-preparable PR:**
1. Update the distribution name in `pyproject.toml` (keep the `import agentloop` package name
   unless a broader rename is approved) and refresh `uv.lock`.
2. Update install commands, README badges, `docs/RELEASING.md`, and `CHANGELOG.md` to use the
   new name consistently.
3. Where install name ≠ import name, document the distinction clearly.
4. Do **not** reference the unrelated PyPI project in a way suggesting affiliation.
5. Verify a clean environment can `pip install`/`uv` the built wheel+sdist by the new name and
   run `agentloop --help` (end-to-end dry run).

## Acceptance criteria (from the issue)

- [ ] A unique final distribution name is selected, checked on PyPI, and reserved by the maintainer. *(owner)*
- [ ] `pyproject.toml`, `uv.lock`, metadata checks, install commands, badges, release docs, and changelog use the name consistently.
- [ ] Documentation distinguishes the install name from `import agentloop` when they differ.
- [ ] The PyPI project is connected via trusted publisher and protected `pypi` environment. *(owner)*
- [ ] A clean environment can install the built wheel/sdist by the final name and run `agentloop --help`.
- [ ] The unrelated PyPI project is not impersonated or referenced as affiliated.
- [ ] The first tag is not created until reservation and end-to-end dry-run validation are complete.

## Testing

- Build wheel + sdist; install each in a fresh venv by the new name; run `agentloop --help`.

## Compatibility / risk

- User-facing install instructions change — coordinate the changelog entry and README badges
  in the same PR so nothing references the old name.
- Blocks any release tag; nothing in Stream G's publish path should proceed until this lands.
