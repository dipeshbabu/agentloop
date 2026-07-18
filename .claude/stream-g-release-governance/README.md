# Stream G — Release & repo governance

## Scope

Getting the project publishable and the public repo hardened. Work spans **`pyproject.toml`**,
**`uv.lock`**, **`.github/workflows/release.yml`**, the issue-form/label YAML in
**`.github/`**, and GitHub **repository settings** (branch protection, security features) — plus
`docs/RELEASING.md` and `docs/OPEN_SOURCE_CHECKLIST.md`, which are the source-of-truth
checklists for this stream.

Issues: **#26** (choose/reserve a PyPI name — P0), **#25** (require full validation before
publish — P0), **#27** (harden repo settings + align labels — P0/P1).

## Approach for the stream as a whole

1. **#26 first, and it's partly a human decision.** The distribution name is a naming/
   compatibility call the maintainer must make and *reserve on PyPI*. An agent can prepare the
   rename PR (pyproject/lockfile/docs/badges) and the trusted-publisher config, but cannot
   pick or reserve the name or flip `PYPI_PUBLISH_ENABLED`. Flag those as owner actions.
2. **#27 next** — mostly GitHub settings (branch protection, private vuln reporting,
   Dependabot security updates, Actions policy) plus small YAML label fixes. Settings changes
   need repo-admin rights; the label/form edits are normal PRs.
3. **#25** — workflow engineering so a tag can only publish a commit that passed the full
   gate. Coordinate with #24 (Stream F) so the container check is included.

Some acceptance items require **owner/admin action outside the codebase** — call these out
explicitly in the PR rather than pretending they're done.

## Stream-specific rules

- Keep GitHub Actions **least-privilege** and **commit-pinned** (the repo already pins SHAs —
  preserve that).
- Do not enable publishing (`PYPI_PUBLISH_ENABLED=true`) until #26's name is reserved and a
  trusted publisher + protected `pypi` environment exist.
- The distribution name and `import agentloop` package name are distinct — document wherever
  they differ; don't rename the import package unless explicitly approved.
- Follow `docs/RELEASING.md` and `docs/OPEN_SOURCE_CHECKLIST.md`; update them as items complete.

## Cross-stream coordination

- **#25 (this stream)** and **#24 (Stream F)** both change CI/release gating — the container
  check #24 adds should be a required prerequisite in #25. Review as a pair.
- #25's "validate the exact tagged commit" should reuse the existing CI workflow rather than
  duplicating gates where possible (one auditable source of truth).

## Definition of done for the stream

All three issues' acceptance criteria met (marking owner-only settings items as done once the
owner confirms); a clean environment can install the built artifact by its final name and run
`agentloop --help`; `main`/tags protected; release publish is impossible unless the exact
commit passed the full gate; checklists updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
