# Remaining product and measurement gaps

AgentLoop can now guide a developer from initialization through a manually
applied change and independently checked before/after evidence. The release
work prioritizes this path over adding another control or experiment system.

| Gap | Current evidence | Next useful validation |
| --- | --- | --- |
| First use by someone unfamiliar with the project | Generated offline onboarding and installed-wheel tests | Observe consenting developers connecting existing agents; record time to first useful finding, failed steps and required help |
| Representative agent work | Small Hermes repository questions, older model/tool studies, and a reproducible three-workload guided suite | Collect coding, retrieval and tool-heavy task sets from actual applications, with owners' independent acceptance criteria |
| Recommendation precision | Native evidence/risk rankings, explicit assumptions, retained false positives and rejected changes | Review recommendations against independent labels; report unsupported recommendations and abstentions alongside accepted ones |
| Quality coverage | Frozen task inputs and declared per-task checks; a wrong fast candidate cannot pass those checks | Expand adversarial and held-out tasks; measure quality dimensions beyond exact structured answers |
| Causal performance | Per-attempt measurements and disclosed fixed-order confounds | Counterbalance conditions, control cache/load effects, and increase independent task/host/model coverage |
| Instrumentation overhead | Recorded tracing controls, with scope and model-variation limits | Separate direct recording, export, analysis and end-to-end overhead across realistic span volumes |
| Cost | Actual token provenance and qualified model-price estimates | Obtain authorized billing data; retain unknown local hardware/energy cost instead of inventing dollars |
| Hermes compatibility | 0.21.6 contract coverage and real 0.22.7/0.23.1 conformance | Revalidate future hook changes against the pinned runtime scenarios before extending the supported range |
| TypeScript adoption | Source SDK, package/declaration/interoperability checks and separate release proposal | Approve and publish the initial npm artifact; test real Node applications and selected browser/bundler targets |

No external users, production deployments or customer savings are established by
these tests and studies. PyPI download counts do not identify unique developers
or deployments. The retained small experiments support their specific observed
outcomes, including failures, without establishing a general optimization rate.

For the next iteration, prioritize the first-use observation and one real
application's held-out correctness suite. Those will show whether developers can
find a useful inefficiency and prove a safe improvement with less assistance.
