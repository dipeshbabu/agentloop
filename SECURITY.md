# Security Policy

## Supported versions

Until AgentLoop reaches 1.0, security fixes are made on `main` and released in
the newest version. Older pre-1.0 versions do not receive guaranteed backports.

| Version | Security fixes |
| --- | --- |
| Latest release | Yes |
| `main` | Yes, before the next release |
| Older releases | No guaranteed backports |

## Report a vulnerability

Do not open a public issue, discussion, or pull request for a suspected
vulnerability.

Use [GitHub private vulnerability reporting](https://github.com/dipeshbabu/agentloop/security/advisories/new).
If that form is unavailable, use the private contact information on the
[maintainer's GitHub profile](https://github.com/dipeshbabu) to request a secure
reporting channel. Do not send exploit details until a private channel is
established.

Include as much of the following as you can:

- the affected version, commit, and component;
- the impact and a realistic attack scenario;
- minimal reproduction steps or a proof of concept;
- any known mitigations; and
- whether you have disclosed the issue elsewhere.

Use synthetic traces and placeholder credentials. A report must not include
customer prompts, production trace data, API keys, access tokens, or other
people's personal information.

## What to expect

The project aims to acknowledge a complete report within three business days and
provide an initial assessment within seven business days. These are response
targets, not a service-level agreement. The reporter will receive updates when
the assessment changes or a fix is ready.

The maintainer and reporter will coordinate disclosure. The project may publish
a GitHub security advisory with affected versions, mitigations, and credit after
a fix is available. Please allow a reasonable remediation window before public
disclosure; 90 days is the usual upper bound unless active exploitation or user
safety requires a different timeline.

## Scope and safe research

Reports about the Python package, API server, dashboard, official container, and
official integrations are in scope. Vulnerabilities that exist only in a
third-party dependency should normally be reported to that dependency first,
though AgentLoop still welcomes reports about exploitable dependency use or a
missing mitigation.

Good-faith research should avoid privacy violations, service disruption, data
destruction, persistence, and access beyond what is needed to demonstrate the
problem. Stop testing and report immediately if you encounter real user data.
The project will not recommend legal action against research that follows these
guidelines and makes a good-faith effort to avoid harm.
