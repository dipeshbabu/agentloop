# Stream G — Release & repo governance

> **Status: ✅ Done**, and then some. All three original issues (#26, #25, #27) are
> closed and shipped in `v0.5.0`. Two follow-on efforts grew out of this stream after
> that and are also done:
>
> - **#52** — automated the version-bump/changelog-rollover/tagging toil this README's
>   "Approach" section originally assumed was manual. See
>   `.github/workflows/bump-version.yml` and `.github/workflows/tag-release.yml`.
> - **Repository hardening** — the owner completed the branch-protection ruleset,
>   tag protection, and SHA-pinning policy this README's "Stream-specific rules"
>   section only asked for informally. See `docs/OPEN_SOURCE_CHECKLIST.md`'s
>   "GitHub repository settings" section for the current, verified state.
>
> Those two shipped independently and interacted: the branch-protection ruleset
> requires named status checks with **no bypass**, but `bump-version.yml` opens its
> release-prep PR with the default `GITHUB_TOKEN`, which GitHub's anti-recursion rule
> exempts from triggering `pull_request`-based checks. `bump-version.yml` now
> explicitly dispatches `ci.yml`, `codeql.yml`, `dependency-review.yml`, and
> `agentloop-performance.yml` against the release branch via `workflow_dispatch` (which
> *is* exempt) so the required checks actually run and report against the PR's head
> commit. If you touch any of those four workflow files or the branch-protection
> ruleset again, re-verify this still closes the loop — it's the one place in this
> repo where two "done" pieces of work depend on each other's internals rather than
> just not conflicting on files.
>
> Kept below as the historical plan for the original three issues — read the current
> `.github/workflows/`, `docs/RELEASING.md`, and `docs/OPEN_SOURCE_CHECKLIST.md` for
> what's actually in place now.

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
