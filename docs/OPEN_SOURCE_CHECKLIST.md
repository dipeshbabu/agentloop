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
- [ ] Enable GitHub private vulnerability reporting and verify that the link in
      `SECURITY.md` opens the private advisory form.
- [ ] Publish a private Code of Conduct reporting address or form and replace the
      fallback contact instructions in `CODE_OF_CONDUCT.md`.

If sensitive history must be rewritten, coordinate the rewrite before launch and
rotate affected secrets first. Rewriting history does not make a leaked secret
safe again.

## GitHub repository settings

- [ ] Add a concise repository description, homepage, and topics such as
      `ai-agents`, `observability`, `profiling`, and `python`.
- [x] Enable the dependency graph and Dependabot alerts.
- [ ] Enable Dependabot security updates. The repository already configures uv,
      pre-commit, Actions, and Docker version updates.
- [x] Enable code scanning so `.github/workflows/codeql.yml` can upload results.
- [ ] Protect `main`: require a pull request, passing CI and security checks,
      resolved review conversations, and no force pushes.
- [ ] Protect release tags matching `v*`.
- [ ] Decide whether GitHub Discussions will be offered for usage questions and
      update `SUPPORT.md` if it is enabled.
- [x] Confirm that issue labels referenced by the forms, Dependabot, and generated
      optimization drafts exist. Generated drafts use the stable `enhancement`
      label and keep finding type/severity in the issue body instead of creating
      dynamic labels.
- [ ] Review Actions permissions and allow only the actions used by the checked-in
      workflows.

### Repository settings target and review record

The settings audit was repeated on 2026-07-18. Code scanning, secret scanning,
push protection, the dependency graph, and Dependabot alerts are enabled. The
repository still has no branch or tag ruleset; private vulnerability reporting
and Dependabot security updates are disabled; Actions currently allows every
source and does not require SHA pinning. These unchecked settings remain owner
actions and must not be reported as complete until verified through GitHub.

Configure a `main` ruleset with these properties:

- require pull requests and resolution of review conversations;
- block force pushes and deletion, with bypass limited to the repository owner
  for documented emergencies;
- require `Python 3.10`, `Python 3.13`, `Docker image and deployment smoke`,
  `Replay and optimization gates`, `Analyze Python`, and
  `Review dependency changes`; add `Package artifact and wheel smoke` when the
  release-validation workflow from issue #25 lands; and
- allow workflows for forked pull requests to run with read-only permissions and
  synthetic credentials, without exposing repository or environment secrets.

Add a second ruleset protecting tags matching `v*`. Restrict Actions to GitHub,
verified creators, and the pinned actions used by this repository, or enable the
repository SHA-pinning requirement. Enable private vulnerability reporting and
verify the `SECURITY.md` advisory link while signed out or as a non-maintainer.

The repository owner should review these controls quarterly and before every
release. Record the date and any approved exception in this section.

## Packaging and releases

- [x] Confirm maintainer control of the existing `agentloop` PyPI project and use
      `agentloop` consistently as the distribution, import package, and CLI name.
- [ ] Configure the PyPI trusted publisher for owner `dipeshbabu`, repository
      `agentloop`, workflow `release.yml`, and environment `pypi`.
- [x] Protect the GitHub `pypi` environment with maintainer approval and a custom
      `v*` tag deployment policy.
- [ ] Set `PYPI_PUBLISH_ENABLED=true` only after the trusted publisher is ready.
- [x] Build the wheel and source archive, inspect their contents, and install each
      in a clean environment.
- [x] Create the first versioned changelog entry.
- [ ] Publish an immutable annotated tag that matches the package version only
      after the trusted publisher and release checks are ready.

## Launch

- [ ] Ask at least one person unfamiliar with the project to follow the README and
      contribution setup from a fresh clone.
- [ ] Triage or remove stale internal issues and verify that public links do not
      depend on private services.
- [ ] Publish a short scope and maturity statement, including the pre-1.0
      compatibility policy and community support boundaries.
- [ ] Monitor issues, security reports, dependency alerts, and CI closely during
      the first public weeks.
