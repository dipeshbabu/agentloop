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
