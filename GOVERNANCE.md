# Governance

AgentLoop uses a lightweight, maintainer-led governance model. The goal is to
make decisions in the open, keep the project technically coherent, and expand
ownership as sustained contributors emerge.

## Roles

### Users

Users run AgentLoop, report problems, request features, and help other users.
There is no formal requirement for this role.

### Contributors

Contributors improve code, tests, documentation, design, or community support.
Contributions are reviewed under the same technical and community standards
regardless of a contributor's employer or affiliation.

### Maintainers

Maintainers triage issues, review and merge changes, manage releases, moderate
community spaces, and protect the project's security and direction. The current
maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md).

## Decisions

Routine decisions happen through issues and pull requests. Maintainers seek
rough consensus and explain material tradeoffs in public. The project lead makes
the final call when consensus cannot be reached, a security response needs a
private decision, or a decision is time-sensitive.

Substantial changes should begin with a public issue. Examples include public API
breaks, data-format or database migrations, new required dependencies, licensing
changes, governance changes, and features that materially change the project's
scope. A proposal should state the problem, alternatives, compatibility impact,
security considerations, and rollout plan.

Decisions may be revisited when new evidence appears. Disagreement with a
technical decision is not a conduct violation; discussion must still follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Becoming a maintainer

Existing maintainers may invite a contributor who has demonstrated, over time:

- sound technical judgment and reliable reviews;
- sustained, constructive contributions;
- care for compatibility, security, documentation, and users;
- consistent Code of Conduct compliance; and
- willingness to handle maintenance work, not only feature work.

The invitation and acceptance are recorded in a pull request updating
`MAINTAINERS.md`. Maintainers may step down at any time. A maintainer who is
inactive for an extended period may be moved to emeritus status after a good-faith
attempt to make contact.

## Releases and security

Only maintainers may create release tags or publish official artifacts. Releases
follow [the release guide](docs/RELEASING.md). Security reports are handled
privately under [SECURITY.md](SECURITY.md), with the smallest practical group of
maintainers involved until coordinated disclosure.

## Conflicts of interest

Maintainers disclose a relevant personal or financial conflict and step back from
the decision when practical. Commercial work built around AgentLoop does not
receive automatic priority over the health of the open-source project.

## Changing this document

Governance changes are proposed by pull request and announced in a linked issue.
Except for urgent corrections, the project should leave a reasonable public
comment period before merging them.
