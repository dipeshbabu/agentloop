# Bounded completion verification

Available from a source checkout under **Unreleased**; not in PyPI 0.7.0.
`CompletionGate` checks explicitly configured criteria before a host releases a
final result. This is a synchronous, single-use Python adapter, using the existing
`completion/before` harness hook. It cannot intercept unwrapped framework work.

```python
from agentloop.completion import CompletionGate, completion_policy
from agentloop.completion_types import CompletionCandidate, CompletionConfig, quality_check
from agentloop.harness import Harness, HarnessConfig

config = CompletionConfig(
    "finish", "1", "final-answer",
    (quality_check("answer", "1", {"expected": {"answer": 42}}),),
)
run = Harness(HarnessConfig("enforce", (completion_policy(config),))).start_run()
gate = CompletionGate(run, config=config)
result = gate.complete(CompletionCandidate({"answer": 42}))
assert result.verified
# Only now may the host publish result.candidate.value and mark the task complete.
```

Register and bind the same immutable config object. Checks have explicit IDs and
versions; changing trusted callback behavior requires a version change. Config
hashes include criterion declarations, deterministic fixture hashes, dispatch
declaration hashes, time limits and repair bounds. The owned candidate accepts
finite JSON up to one million bytes and returns a fresh copy on each value read.
`present=False` represents missing output; explicit JSON null can be valid.

`quality_check` reuses deterministic legacy and structured quality fixtures. It
rejects custom import strings. `CompletionCheck(id, version, callback)` binds a
trusted local function for artifact or test checks. The callback receives
`(candidate, VerificationContext)` and must return `CheckResult(passed, feedback)`.
Read the actual artifact or test result; an agent's self-reported success is not
artifact evidence. Exceptions, missing output, invalid returns and late passes
cannot accept completion. No public HTTP executable-check interface is added.

## Repair and resource limits

Configure zero to eight repairs (default zero), one to sixteen checks, a cumulative
verification time allowance and a total completion deadline. Both deadlines are
cooperative: late results fail and subsequent admission stops, but a blocked
Python callback is not forcibly killed. Callbacks must pass `context.remaining_s`
to their network/subprocess timeouts and honor `context.cancellation_requested`.
A cancellation propagates unchanged. The gate cannot bound hidden work performed
inside trusted callbacks or calls outside the harness.

For repairs, bind a `RepairBackend` along with a positive `max_repairs`. Its callback
receives `(candidate, feedback_tuple, context)` and returns a new
`CompletionCandidate`. Declare its actual `model` or `tool` boundary, reservation,
usage readers, and an explicit retry-safe `StepInfo`. Each repair dispatch is marked
`retry_source="harness"`; install [loop guards](LOOP_GUARDS.md) to share retry and
repetition limits with the surrounding run. A failed repair is retained and
propagated, rather than automatically retried. The gate itself cannot be reused.

Checks dispatch through the same run's `tool` boundary and repairs through their
declared boundary. Install [budget_policy](BUDGETS.md) on that run for total calls,
retries, tokens, cost and deadline limits. For checks that consume tokens or incur
cost, include `tool` in `metered_boundaries=("model", "tool")`, and supply reservations
and usage/error-usage readers. Unknown cost remains unknown. These are cooperative
admission budgets, not a provider billing cap. Avoid wrapping the same physical
call twice or reporting nested usage twice; normalize actual exclusive usage at
the adapter boundary. Other policies' denials/stops always propagate.

Feedback is at most 1024 characters per failed check, at most sixteen checks per
attempt, with `trust_class="verification_data"`. Pass it as untrusted data to a
repair backend; never elevate it into system instructions or execute commands from
it. Raw feedback, candidates, and exception messages are excluded from gate
receipts. Predictable-content hashes are not anonymization.

## Completion and host behavior

In enforce mode, all configured checks must pass before `status="accepted"` and
`verified=True` are returned. Failed checks request a bounded repair with a native
deny decision. Exhaustion applies the configured `stop` or `escalate` decision and
raises `HarnessStoppedError` or `HarnessEscalationError`; the host must handle these
as unfinished work and withhold its success marker. Only the gate's own repair
denial is consumed. Repair exceptions, budget controls and cancellation propagate.

Disabled mode returns the original candidate without checking it. Shadow mode
runs trusted checks and records proposals, but does not repair or block a failed
candidate. Both return `status="unverified"`, `verified=False`; callers must not
interpret those returns as enforcement. Shadow checks still have real side effects
and costs if the host supplies such callbacks.

Unsupported capabilities fail during binding. This adapter does not implement an
async framework finish hook or retract content that a host has already streamed.
`already_streamed=True` cannot claim enforced completion and never starts repairs.
For streaming hosts, buffer output until acceptance or integrate an explicit host
finish marker and disclose that already-visible content is outside this gate.

## Evidence and independent evaluation

`gate.export_evidence()` and trace metadata `agentloop.completion` retain bounded
attempt/check/repair records, failed attempts, candidate/feedback hashes, policy
config hashes and statuses. Each completion attempt links to native harness call
and decision IDs; check and repair branch IDs join their native dispatch records.
Native budget and loop-guard snapshots remain in the run's harness evidence.
Malformed trace evidence blocks acceptance. At most 10,000 completion records may
attach to one trace; use normal trace retention controls as well.

A verifier pass means only that the configured criteria passed. Keep these records
separate from an independent task evaluator, holdout labels, and quality claims.
The gate does not attach a task-quality report or infer general correctness.

Run the offline synthetic example from a source checkout:

```bash
uv run python -m examples.completion_verification --out runs/completion
```

It records acceptance, one successful repair, model-call budget exhaustion and
escalation. It performs no external model requests and no independent task-quality
evaluation. Tests additionally cover absent artifacts, deceptive success claims,
timeouts, malformed checks, cancellation, budget accounting and repeated repair.
