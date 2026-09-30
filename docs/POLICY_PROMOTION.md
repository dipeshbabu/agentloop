# Reviewed budget-policy promotion

Available from an **Unreleased source checkout**, not PyPI 0.7.0. This is an offline
review and bounded canary API for one policy family: built-in Python admission
budgets. It does not learn, change code, activate a policy from historical pass
rates, or deploy anything by itself.

The first supported baseline is a reviewed tracing-only configuration:
`HarnessConfig("disabled")`, with ordinary tracing left enabled by the host.
`BudgetProposal` requires `baseline=BudgetLimits()` to identify that baseline;
existing active budget baselines need a separately matched comparison contract
and are rejected here. Static model/tool/iteration/retry/token/cost limits are
supported. Absolute process-clock deadlines are not portable promotion settings.

## Freeze and assess a candidate

Create a `BudgetProposal` from owned, finite JSON artifacts:

- A frozen native `AblationProtocol`, all original observations, and native harness
  evidence keyed by each shadow/enforce observation's trace ID. Policy references
  use `policy.version + ":" + policy.config_hash`. Conditions must match the exact
  proposed budget policy, including its configuration hash.
- A calibration report produced by `summarize_calibration` and an explicit cohort
  ID. Its artifact hash binds all supplied content; the cohort must match policy,
  workload, model/provider, environment, scorer and quality gate. Preserve the
  original source artifacts for operator review.
- `CapabilityEvidence` naming the Python adapter, source version, tested policy
  hash, test artifact hash, timestamp, actual pass flag and check count.
- Explicit `PromotionThresholds`, `CanaryLimits` and expiry. The proposal hash
  includes evidence, versions, thresholds, baseline and canary limits.

`assess_promotion(proposal)` recomputes native ablation results from observations.
Eligibility requires all planned outcomes, complete independent quality, matching
native decisions and exact policy versions. Decision coverage checks before/after
records, trace IDs, mode, dispatched model/tool counts, capture errors and timing.
A one-second timestamp tolerance permits clock precision differences; hashes and
timestamps do not prove authentic preregistration or truthful measurements.

The minimum is **20 independent held-out tasks and 20 calibration tasks**; callers
cannot lower that floor. Defaults also require 40 held-out pairs. Repetitions do
not create independent tasks. Each cache stratum must meet the thresholds. The
bootstrap requires at least 200 samples and 95% confidence; the proposal bounds
offline study size and resampling work. These are conservative engineering gates,
not a universal sample-size or causal-identification guarantee.

All held-out quality pairs must pass the configured quality floor and regression
limit. The upper latency-delta interval against **tracing**, including policy
overhead, must show the required improvement. Shadow latency has its own overhead
limit and must have complete decision coverage. Shadow behavior alone is never
treated as counterfactual performance evidence.

This first contract requires complete **provider-reported cost** on both sides of
every selected pair and the matching calibration cohort. Estimated, calculated,
mixed, missing or unknown costs cannot authorize enforcement here. Zero paid API
spend on a local model does not establish zero operating cost.

Calibration recomputes task-weighted absolute prediction error from supplied
held-out registrations, retains their denominators and rejects missing, synthetic,
unverifiable, mismatched or too-small cohorts. It checks the current clock against
calibration freshness and expiry instead of trusting a saved `expired=False` flag.
The full report and all rejected/unfinished observations remain reviewable.

The assessment returns explicit reasons and `enforcement_authorized=False`, even
when eligible. Evidence authenticity, permission, scorer validity and provenance
remain the trusted host/operator's responsibility. A checksum is not an operator
signature, and the API does not authenticate a person named in an approval.

## Review, opt-in and bounded canary

```python
from agentloop.policy_promotion import BudgetPromotion, CanaryOutcome

control = BudgetPromotion(proposal, baseline_review_ref="reviewed-tracing-v1")
shadow_config = control.shadow_configuration()
# The host runs/reviews shadow evidence, then authenticates a maintainer/operator
# attestation bound to proposal.proposal_hash, approved scope and expiry.
assessment = control.review(operator_approval)
if assessment["eligible"] and assessment["operator_approved"]:
    control.begin_canary()
    lease = control.admit(
        scope="approved-workload-scope", versions=current_versions,
        workload_id=current_workload_id, opted_in=True,
    )
    # Apply lease.configuration to the actual host run. Report every candidate
    # lease once, including failures, cancellation and unknown measurements.
    if lease.candidate:
        control.report(lease, independently_measured_outcome)
```

`OperatorApproval` must explicitly approve the exact proposal hash, name a reviewer
and review reference, declare bounded scopes and provide approval/expiry times.
Rejected, mismatched, future, expired or pre-evidence approvals cannot enforce.
The result reports eligibility and operator approval separately. The host must
authenticate and retain the actual approval before constructing this attestation;
there is no public HTTP approval shortcut or default approval.

Every admission requires a matching workload ID, exact frozen runtime versions,
approved scope and explicit opt-in. A model, framework/runner, scorer, environment,
tool, source or workload change rolls back the active canary. The host supplies
truthful versions; the controller does not inspect hidden SDK calls automatically.

Canaries admit at most 1,000 runs for at most one day, with caller-selected lower
limits. Each run reports status, independent quality, latency and known cost.
Any failed or missing measurement, quality loss, latency/cost threshold violation,
expiry or backwards/unavailable clock rolls back future admissions. All outcomes,
including late reports after rollback, remain in the denominator. Missing reports
cannot count as success. Native evidence failures also block admission.

Call `control.tick()` during idle periods; time limits are checked on tick,
admission and reporting. Set `control.kill_switch` or call `control.rollback()`
for immediate control of future admission. Already-running work is not killed or
undone. Restarted controllers need a new review; no durable rollout store or lease
recovery mechanism is added.

A completed passing canary transitions to `canary_passed` and returns to the last
reviewed baseline for future work. It does **not** expand into unbounded production
enforcement. A further rollout is a new operator decision with renewed evidence
and explicit scope. Rollback returns the exact reviewed tracing-only configuration.

## Audit and examples

`control.export_evidence()` retains all transitions, admissions, outcomes, missing
counts, policy/proposal hashes and merged native harness decision evidence, even
without an active trace. With tracing, `agentloop.policy_promotion` links those
records to native #184 decisions. Raw prompts and outputs are not retained.
The host should persist exports and approval artifacts according to its audit
policy. Evidence and runtime leases are bounded in memory.

Run the deliberately ineligible offline example:

```bash
uv run python -m examples.policy_promotion --out runs/policy-promotion
```

It reuses the existing synthetic ablation fixtures, preserves their missing and
failed outcomes, and writes rejection reasons. It neither fabricates empirical
approval nor activates a canary. Unit tests use explicitly simulated inputs to
exercise eligible and ineligible transitions, rollback, concurrency and expiry.

The recorded [real harness study](REAL_HARNESS_STUDY.md) retains quality failures,
and the [empirical calibration](EMPIRICAL_CALIBRATION.md) retains abstentions and
unknown costs. Implementing this lifecycle does not make those candidates eligible
or authorize their activation. See also [budgets](BUDGETS.md),
[ablation evidence](HARNESS_ABLATIONS.md) and [native decision evidence](HARNESS_EVIDENCE.md).
