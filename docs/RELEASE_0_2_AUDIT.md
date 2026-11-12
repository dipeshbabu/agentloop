# AgentLoop 0.2.0 readiness audit

Reviewed on 2026-11-12 against main `017a7bdd9818f08d832c02c631b18c1eb08fdb19`.
The latest published Python and GitHub release is 0.1.1. There are 29 changed
files since its tag. No open Dependabot or CodeQL alerts were reported by GitHub
at this review. The working tree was clean and there were no open pull requests.

## Smallest implementation sequence

1. Verify installed-wheel guided commands and failure contracts. Keep ordinary
   release CI deterministic and make local-model experiments an explicit dispatch.
2. Add guided project initialization, actionable errors, and visible native
   evidence/risk/savings provenance. Test the complete generated onboarding path.
3. Add reproducible repository, retrieval and tool-heavy pilot/held-out validation
   using native replay, intervention and study artifacts. Preserve negative results.
4. Prepare 0.2.0 with the existing version helper on a separate release branch.
   Dependent pull requests remain stacked and require approval before integration.
5. Audit the TypeScript package independently and prepare its publishing proposal.

## Verified release gaps

| Area | Finding and required verification |
| --- | --- |
| Installed package | The existing wheel smoke did not run guided capture/start/finish. Add isolated external-directory round trips and retained negative cases. |
| Cancellation | A child exiting with the conventional interrupt status was classified as failed. Preserve cancellation distinctly. |
| CI reliability | The normal PR workflow ran local model inference and required a timing-dependent optimization outcome. Gate this study behind an explicit manual input. |
| First use | No `improve init` command or generated Python adapter exists. |
| Recommendation trust | Native rankings and estimate provenance exist, but the guided plan omits much of their risk, readiness and savings detail. |
| Errors | Generic capture/rejection messages obscure the responsible task, gate and missing evidence. |
| Validation breadth | Hermes evidence covers three public metadata questions repeated twice. Broader agent workloads and held-out evidence remain to be evaluated. |
| TypeScript | SDK 0.1.0 remains unpublished. Its metadata, tarball installation and API compatibility require a separate readiness record. |

## Compatibility and release boundaries

Python 0.1.1 already requires Python 3.11+. Native trace, study, receipt, API and
database versions remain independent of the 0.2.0 package version. Guided APIs
are currently source capabilities. Existing explicit `--tasks` and runner argv
must continue to work after initialization is added.

The source-exported Hermes plugin accepts `>=0.21.6,<0.23`; v0.22.7 is pinned for
runtime checks and v0.21.6 has contract coverage. Current upstream v0.23.1 is
outside this declared range. Release documentation must state that limitation
until separate compatibility testing supports expanding it.

The earlier local CPU Hermes study is a controlled example with fixed order,
three distinct questions and unknown operating costs. Its measurements do not
establish developer adoption, production performance or calibrated savings.

Full test/lint/build results, exact CI commit references, remaining measurement
gaps and release approvals will be recorded in the preparation PR. Publication
requires an explicit later approval. No release tag or publishing action is part
of this audit.

## Preparation review

The focused stack is [#305](https://github.com/dipeshbabu/agentloop/pull/305)
(installed-package/failure validation),
[#307](https://github.com/dipeshbabu/agentloop/pull/307)
(initialization, trustworthy decisions and onboarding), and
[#308](https://github.com/dipeshbabu/agentloop/pull/308)
(three-workload validation and retained negative evidence). The separately
reviewable [SDK proposal #306](https://github.com/dipeshbabu/agentloop/pull/306)
does not block Python preparation. All remain drafts requiring maintainer review
and approval; no integration, release tag or publication has been authorized.

The 0.2.0 branch uses `scripts/bump_version.py --set 0.2.0`, updating package/runtime
metadata, the dated changelog and only the project version in `uv.lock`. The
dependency set is unchanged. Source metadata names Dipesh Tharu Mahato as author.
README and upgrade notes distinguish 0.2.0 source capabilities from published
0.1.1. No package, trace, task, session, API or database numbering reset occurs.

| Verification | Result before final release-head CI |
| --- | --- |
| Full onboarding source suite | 2,731 passed, 108 skipped locally; final Python 3.11/3.13/3.14/3.15 CI passed with PostgreSQL |
| Lint/security/metadata/links | Full pre-commit checks passed for the focused implementation branches |
| Distribution compatibility | Built wheel/sdist passed Twine; a fresh core-only installed wheel ran all guided commands outside the checkout, including generated onboarding, quality rejection, incomplete capture, missing usage and cancellation |
| Existing frozen session | Original pre-change Hermes session recomputed with 6/6 passing pairs |
| Hermes host conformance | Real pinned 0.22.7 dispatch covers eight deterministic scenarios in normal CI; real-model studies require explicit opt-in |
| Guided validation | Six dedicated tests passed, including actual LangGraph 1.2.11 and archived evidence recomputation |
| Fresh real-model study | 45 captured attempts plus 12 overhead controls retained; candidate rejected, with only 3/12 held-out pairs passing every gate |
| Evidence retention | 300-file checksummed archive unpacked and native finish reproduced the 3/12 result without inference |
| Dependency/security state | pip-audit: 77 locked development/all-extra packages, no skips or known advisories; GitHub: zero open Dependabot/CodeQL alerts at recheck |
| TypeScript | 24 tests, declarations, packed installation and authenticated API interoperability passed; 21 final-commit CI checks passed on its separate proposal |

The release PR records the exact final 0.2.0 test, lint, build, installed-wheel,
artifact and CI commands/results. Focused earlier checks do not replace final
release-head checks or required reviews. Source review found and addressed
cancellation classification, generated-directory traversal, intervention gate
consistency and actionable-error gaps. The skipped local cases include optional
backends/integrations; CI supplies PostgreSQL and pinned framework coverage.

## Remaining blockers and evidence limits

- The final release PR must satisfy its protected-branch checks and required
  reviews, and the maintainer must authorize integration and publication.
- Published 0.1.1 users cannot use guided optimization until they install reviewed
  source or the approved 0.2.0 release. No tag or PyPI write is part of preparation.
- The new small-model pilot failed correctness; the held-out candidate was
  rejected. Its qualified overhead estimate is unavailable. It must not be
  presented as a successful optimization or a reason to deploy the agent.
- The historical Hermes success remains six comparisons across three questions,
  with fixed order and unknown operating cost. It is separate from the negative
  [new trial](GUIDED_LOCAL_MODEL_CASE.md).
- Hermes 0.23.x, external developer onboarding, production effects, independent
  application task sets, billing and causal performance/overhead evidence remain
  unestablished. See [adoption gaps](ADOPTION_GAPS.md).
- npm publication, registry ownership and browser/bundler validation remain
  separate decisions. Neither npm nor PyPI publication is implied by passing CI.
