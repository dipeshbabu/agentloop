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
- [ ] Enable the dependency graph, Dependabot alerts, and Dependabot security
      updates. The repository already configures uv, pre-commit, Actions, and
      Docker version updates.
- [ ] Enable code scanning so `.github/workflows/codeql.yml` can upload results.
- [ ] Protect `main`: require a pull request, passing CI and security checks,
      resolved review conversations, and no force pushes.
- [ ] Protect release tags matching `v*`.
- [ ] Decide whether GitHub Discussions will be offered for usage questions and
      update `SUPPORT.md` if it is enabled.
- [ ] Confirm that issue labels referenced by the forms and Dependabot exist, or
      adjust the YAML files to match the chosen label set.
- [ ] Review Actions permissions and allow only the actions used by the checked-in
      workflows.

## Packaging and releases

- [ ] Resolve the PyPI name collision described in `docs/RELEASING.md`. Update the
      distribution name in `pyproject.toml` without changing the `agentloop`
      import package unless a broader rename is intentional.
- [ ] Reserve the final distribution name and configure a PyPI trusted publisher
      for this repository and its `pypi` environment.
- [ ] Add environment protection rules for publishing, then set
      `PYPI_PUBLISH_ENABLED=true` only when publishing is ready.
- [ ] Build the wheel and source archive, inspect their contents, and install each
      in a clean environment.
- [ ] Create the first versioned changelog entry and publish an immutable annotated
      tag that matches the package version.

## Launch

- [ ] Ask at least one person unfamiliar with the project to follow the README and
      contribution setup from a fresh clone.
- [ ] Triage or remove stale internal issues and verify that public links do not
      depend on private services.
- [ ] Publish a short scope and maturity statement, including the pre-1.0
      compatibility policy and community support boundaries.
- [ ] Monitor issues, security reports, dependency alerts, and CI closely during
      the first public weeks.
