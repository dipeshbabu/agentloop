# Open-source launch checklist

Repository files can establish the public rules, but several legal and GitHub
settings still require an owner to review or enable them. Complete this checklist
before announcing the public launch, or address it immediately if the repository
is already public.

## Legal and provenance

- [ ] Confirm that the copyright owner has the right to release every source,
      test, example, document, and asset in the repository.
- [ ] Review employer, contractor, customer, and prior-project agreements for
      ownership or confidentiality restrictions.
- [ ] Confirm Apache-2.0 is the intended license. If not, update `LICENSE`,
      `pyproject.toml`, the README, contribution terms, and dependency policy
      together.
- [ ] Inventory copied or generated third-party material. Preserve required
      attribution and add a `NOTICE` file only when an included work requires it.
- [ ] Review dependency licenses for compatibility with distribution and the
      project's intended commercial use. Refresh and verify the inventory in
      `THIRD_PARTY_LICENSES.md` from a clean locked environment.

This checklist is operational guidance, not legal advice. Ask qualified counsel
to review ownership or licensing questions that are unclear.

## Secrets, privacy, and history

- [ ] Scan the full Git history, not only the current checkout, with a dedicated
      secret scanner.
- [ ] Rotate any credential that was ever committed, even if the commit was later
      removed.
- [ ] Confirm that examples, tests, screenshots, databases, and traces contain no
      real prompts, customer data, internal URLs, tokens, or personal information.
- [ ] Review `.env.example`, Docker defaults, workflow permissions, and production
      documentation for safe public defaults.
- [x] Enable GitHub private vulnerability reporting and verify that the link in
      `SECURITY.md` reaches the private advisory flow. A signed-out request is
      redirected to GitHub login with the advisory URL preserved as the return
      target.
- [ ] Publish a private Code of Conduct reporting address or form and replace the
      fallback contact instructions in `CODE_OF_CONDUCT.md`.

If sensitive history must be rewritten, coordinate the rewrite before launch and
rotate affected secrets first. Rewriting history does not make a leaked secret
safe again.

## GitHub repository settings

- [x] Add a concise repository description, the official PyPI project as the
      homepage, and the `ai-agents`, `observability`,
      `performance-engineering`, `profiling`, and `python` topics.
- [x] Enable the dependency graph and Dependabot alerts.
- [x] Enable Dependabot security updates. The repository already configures uv,
      pre-commit, Actions, and Docker version updates.
- [x] Enable code scanning so `.github/workflows/codeql.yml` can upload results.
- [x] Protect `main`: require a pull request, passing CI and security checks,
      resolved review conversations, and no force pushes.
- [x] Protect release tags matching `v*` against deletion and movement.
- [x] Keep GitHub Discussions disabled for now; `SUPPORT.md` directs usage
      questions and feature proposals to the existing public issue forms.
- [x] Confirm that issue labels referenced by the forms, Dependabot, and generated
      optimization drafts exist. Generated drafts use the stable `enhancement`
      label and keep finding type/severity in the issue body instead of creating
      dynamic labels.
- [x] Require every external GitHub Action and reusable workflow to be pinned to
      a full commit SHA. All checked-in workflows satisfy this policy.

### Repository settings target and review record

The owner completed and verified the settings audit on 2026-07-18:

- the active `Protect main` ruleset requires pull requests, resolved review
  conversations, strict up-to-date status checks, and blocks deletion and
  non-fast-forward pushes. It requires `Python 3.10`, `Python 3.13`,
  `Docker image and deployment smoke`, `Package artifact and wheel smoke`,
  `Replay and optimization gates`, `Analyze Python`, and
  `Review dependency changes` from GitHub Actions;
- the active `Protect release tags` ruleset covers `refs/tags/v*` and blocks
  deletion and non-fast-forward updates;
- neither ruleset has a standing bypass actor. The owner can change the ruleset
  itself for a documented emergency, which keeps normal pushes and tag changes
  subject to the same controls as contributor changes;
- private vulnerability reporting, the dependency graph, Dependabot alerts,
  Dependabot security updates, code scanning, secret scanning, and push
  protection are enabled;
- the `SECURITY.md` advisory URL was checked signed out and reached GitHub login
  with the private advisory URL preserved as its return target; and
- Actions remains available to the project's required sources, but GitHub now
  rejects any external action or reusable workflow that is not pinned to a full
  commit SHA. Forked pull-request workflows retain read-only permissions and do
  not receive repository or environment secrets.

The owner must review these controls quarterly and before every release. The
next scheduled review is 2026-10-18. Record the date and any approved exception
in this section.

## Packaging and releases

- [x] Select the unique `agentloop-profiler` distribution name while retaining
      `agentloop` as the import package and CLI name.
- [x] Configure and verify the `agentloop-profiler` trusted publisher for owner
      `dipeshbabu`, repository `agentloop`, workflow `release.yml`, and
      environment `pypi`.
- [x] Protect the GitHub `pypi` environment with maintainer approval and a custom
      `v*` tag deployment policy.
- [x] Set `PYPI_PUBLISH_ENABLED=true` after the trusted publisher was ready.
- [x] Build the wheel and source archive, inspect their contents, and install each
      in a clean environment.
- [x] Create the first versioned changelog entry.
- [x] Publish immutable annotated tag `v0.4.0` after the trusted publisher and
      exact-commit release checks passed.

## Launch

- [ ] Ask at least one person unfamiliar with the project to follow the README and
      contribution setup from a fresh clone.
- [ ] Triage or remove stale internal issues and verify that public links do not
      depend on private services.
- [ ] Publish a short scope and maturity statement, including the pre-1.0
      compatibility policy and community support boundaries.
- [ ] Monitor issues, security reports, dependency alerts, and CI closely during
      the first public weeks.
