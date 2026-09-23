# Opt-in Python harness contract

The harness controls only callables explicitly wrapped by the host application.
Creating a run does not intercept other model, tool, framework, or provider calls.
Ordinary AgentLoop tracing has no harness dependency and remains unchanged.

```python
from agentloop.harness import Decision, Harness, HarnessConfig, Hook, Policy

guard = Policy(
    policy_id="reviewed-tool-denial",
    version="1",
    evaluate=lambda context: Decision("deny", "requires_review"),
    hooks={Hook("tool")},
    actions={"deny"},
)
run = Harness(HarnessConfig(mode="shadow", policies=(guard,))).start_run("task-01")
protected_tool = run.wrap(lambda: "result", boundary="tool", branch_id="lookup")
assert protected_tool() == "result"  # Shadow records the denial but admits the call.
assert run.results[0].action == "deny"
assert run.results[0].applied is False
```

Use `mode="enforce"` only after reviewing policy configuration and the adapter's
coverage. The same call then raises `HarnessDeniedError` before dispatch. Handle
`HarnessDeniedError`, `HarnessStoppedError`, and `HarnessEscalationError` in the
host application's control flow. Roll back by creating a disabled configuration;
an existing run retains its immutable settings and decisions.

Run `uv run python examples/harness_controls.py` for the three modes together.
The disabled and shadow cases each dispatch once; the enforced case dispatches
zero times. These synthetic cases verify control flow, not policy usefulness.

## Contract 1.0

`HarnessConfig` fixes a mode (`disabled`, `shadow`, or `enforce`) and an ordered
set of versioned policies. Disabled is the default. Each policy declares its
before/after hooks, possible actions, priority, and JSON configuration. Settings
are deeply immutable; canonical hashes cover the configuration, policy identity
and version, ordering, and mode. A hash identifies settings, not callback code or
anonymous data. Change a policy's version when its implementation changes.

`Harness.start_run()` returns explicit, run-local state. There is no implicit
process-global active run. A host shares one run across concurrent branches and
assigns branch IDs when wrapping functions, or creates separate runs for separate
executions. Hook callbacks are synchronous, bounded, and serialized within a run;
protected work executes outside that lock. Callbacks must use their supplied
policy state rather than mutable globals and must not retain that state beyond
the hook. Calling a protected wrapper from a policy is rejected to prevent
recursive policy execution. This is a cooperation contract, not a sandbox for
untrusted policy code.

Hooks cover `model`, `tool`, `iteration`, and `completion`. The host defines the
corresponding dispatch boundary; an iteration is not inferred from repeated tool
names. Before hooks run before invoking the callable. After hooks receive an
execution status, including denial, failure, cancellation, and stream closure;
they do not receive raw arguments, results, or exception messages. Existing trace
and parent IDs are read through public trace-context helpers, without taking over
tracing or changing the active context.

`HookContext.dispatched` and `HookResult.dispatched` are false before admission
and on cleanup after a failed/denied before hook. They are true once the wrapper
attempts to invoke the callable. This distinguishes a policy cancellation before
dispatch from cancellation of admitted work, without inferring provider usage or
side effects. Even an attempted invocation can fail before the callable's body
does any work (for example, argument binding can fail).

Policies run by ascending priority, then policy ID. All matching policies are
evaluated; conflict resolution is `escalate > stop > deny > continue`. An explicit
adapter capability declaration is checked at construction. Enforced hooks or
actions outside those capabilities are rejected before any work starts. Returning
an undeclared action is a policy error, not implicit permission to dispatch.
Enforced adapters must also support stop and escalation for intrinsic policy
failure handling, even when the configured policy normally only continues.

| Action | Enforced behavior |
| --- | --- |
| `continue` | Admit this call or preserve its result |
| `deny` | Reject this call before dispatch; other calls remain eligible |
| `stop` | Stop admission for this run and raise a control exception |
| `escalate` | Stop admission and raise an escalation for the host to resolve |

`deny` is only valid for before hooks. An after-hook stop/escalation cannot undo
completed work and may prevent returning its result. If the protected call itself
raises or is cancelled, that original exception is preserved even when an after
hook fails; the hook failure is recorded and enforced future admission stops.
Already admitted work is not forcibly interrupted by a later stop.

In shadow mode, decisions and policy failures are observable but do not deny,
stop, reroute, retry, or change the callable's return value. Shadow policy state
is hypothetical and run-local. In enforce mode, policy errors stop admission and
raise escalation rather than silently allowing protected work. Cancellation and
other process-control exceptions retain their original identity.

## Streaming and boundaries

Python wrappers distinguish ordinary, coroutine, generator, and async-generator
functions. Generator admission is lazy: no policy or callable executes until the
first resume. Send, throw, close, async equivalents, and generator return values
are preserved. After hooks run once, at exhaustion, closure, failure, or denial.
A generator never resumed produces no decisions. Disabled wrappers return the
original callable, preserving all existing behavior and instrumentation.
Wrapping the same protected function again with the same run/boundary/branch is
a no-op. Metadata copied onto a different function does not bypass its admission.

A normal function that returns an SDK stream is controlled only through its
return, not through subsequent consumption. Wrap an explicit generator that
delegates to that stream to control its full lifetime. Wrappers do not claim
coverage of hidden provider retries, arbitrary blocking code, remote side effects,
or unsupported framework dispatch. Stopping is cooperative; there is no process
termination, automatic retry, or spend guarantee in this contract.

## Evidence and extensions

The run exposes immutable in-memory hook results and versioned
[decision evidence](HARNESS_EVIDENCE.md). Active traces retain policy snapshots,
proposal dispositions, dispatch status, timing, and explicit retry/conflict links
through native JSON, supported OTLP round trips, and HTML. The existing intervention
ledger links actual comparisons without treating a logged action as a measured
benefit. Raw policy configuration capture is disabled by default.

[Admission budgets](BUDGETS.md) use the same hooks for atomic reservation and
reconciliation, with explicit unknown-usage policies and cooperative deadlines.
`DispatchOptions` carries typed bounds and retry origin; an optional synchronous
`usage_reader` returns normalized `ResourceUsage` without retaining raw results.
[Retry and loop guards](LOOP_GUARDS.md) use explicit step identities and caller
fingerprints, with bounded retry and optional repetition/oscillation rules.
`Decision.retry_of` annotates a host-declared relationship; nothing automatically
executes a retry or replans the workflow.

Policy transformations, provider routing, scheduling, checkpoint recovery, and
automatic completion repair require later explicit capability extensions. The
initial adapter supports only the actions above and introduces no required SDK.
