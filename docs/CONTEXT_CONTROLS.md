# Explicit context transformations

This opt-in API is **Unreleased** and requires a source checkout. Ordinary tracing
does not rewrite prompts. The first supported transformation summarizes only
tool results that the caller explicitly marks optional. It does not choose a
policy, identify optional evidence, trim instructions or activate itself.

```python
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_controls import ContextModel, context_policy
from agentloop.context_types import (
    ContextAdapter, ContextPolicyConfig, ContextRequest, SummaryBackend,
)
from agentloop.harness import Harness, HarnessConfig

settings = ContextPolicyConfig(
    "old-search-summary", "1", "answer", "my-independent-scorer-v1",
    max_summary_chars=512, max_summaries=1, on_invalid="error",
)
run = Harness(HarnessConfig(
    mode="shadow",
    policies=(context_policy(settings), budget_policy(BudgetLimits(max_model_calls=2))),
)).start_run("task-01")
model = ContextModel(
    run, my_synchronous_provider,
    config=settings,
    adapter=ContextAdapter("my-text-chat-adapter"),
    summarizer=SummaryBackend("my-summary-model-and-prompt-v1", my_summarizer),
    usage_reader=my_normalized_usage_reader,
)
answer = model(ContextRequest(
    provider_payload,
    optional_tool_results=("old-search-call",),
    protected_tool_results=("current-evidence-call",),
))
```

The host supplies the three callbacks and an actual provider payload in this
integration sketch. `my_summarizer` receives `SummaryInput` with `content`,
`max_chars`, `source_hash` and the fixed trust class `tool`. It returns
`SummaryResult(text, usage)`, where usage is a normalized `ResourceUsage` for that
one summary call. The provider receives an owned copy of the JSON payload and
returns its usual non-streaming result. The usage reader reports only that
provider call, excluding its child summaries. Never call another model outside
the supplied run to hide summary usage.

The supported message format is a flat `messages` list with textual system,
developer, user, assistant and tool messages. Assistant tool calls may have null
content. Every declared tool call must have exactly one result before the next
non-tool message. The original roles, call declarations, IDs, all other messages
and all provider options retain their values and dictionary ordering. Protected
results take precedence over optional selections. Images, streaming requests and
async providers are unsupported in the first adapter; unsupported transformations
produce an explicit error or the configured original-request fallback.

The capability declaration is a host integration contract, not automatic SDK
detection. A matching `context_policy` must be registered on the same harness.
Before-model admission binds one explicit adapter invocation; generic calls on
its selected branch cannot borrow the transformation admission. The summary runs
after admission and outside policy callbacks. Policies receive metadata rather
than raw request content.

## Modes, validation and rollback

- **Disabled:** pass an owned original payload to the provider; no summary or
  transformation receipt is created.
- **Shadow:** validate and record a proposal, then send the original payload.
  No summary is executed, including for malformed or unsupported proposals.
- **Enforce:** summarize selected long tool results, validate the complete
  candidate, then invoke the provider. Change modes by creating a new run.

Caller history is never mutated. A summary must be nonempty, shorter than the
source and within `max_summary_chars`. At most `max_summaries` results may be
selected; excess targets fail before any summary call. Summaries replace only
the content of their original tool message. Text that resembles an instruction
stays tool data. The host must preserve that trust class inside its summarizer
too; structural preservation does not prove semantic fidelity or immunity to
prompt injection.

`on_invalid="original"` restores the whole original request after an invalid
candidate, including when a later summary fails. It cannot restore a partially
modified request. Already incurred summary usage remains charged. A stopped run,
cancellation, a streaming request or an unverified configured context limit does
not receive this fallback. Provider failures are propagated without automatic
retry. Disabling the policy is the rollback; it cannot undo a completed call.

## Usage, limits and evidence

Main and summary calls share the same `HarnessRun` and its [admission
budgets](BUDGETS.md). Supply `DispatchOptions(Reservation(...))` for **both** the
main provider and `SummaryBackend` when enforcing token or cost budgets. Bounds
must include input and maximum output for the respective call. The parent
reservation is acquired before preparing summaries, so simultaneous reservations
are conservative. Model-call admission can count an attempted preparation even
if a child summary blocks actual provider invocation. The receipt separately
records `provider_invoked`; it is not proof of a remote network dispatch.

Summary `ResourceUsage` includes normalized tokens and cost with their
provenance. Unknown usage follows the budget's existing unknown-usage policy;
missing cost is never inferred to be zero. Do not count a provider's inclusive
parent usage again as exclusive summary usage.

Optional `token_counter(payload)` returns `ContextTokenCount`. Without it,
pre-dispatch token counts are explicitly unavailable. A configured
`max_input_tokens` requires compatible exact `provider`, `tokenizer` or
`user_supplied` counts, with a versioned reference. Word estimates cannot satisfy
that bound. If exact before/after counts show no token reduction, the candidate
fails validation. The host is responsible for using the provider's actual chat
template and counting all relevant request fields. Character reduction alone
does not establish a token reduction or quality preservation.

`model.export_evidence()` and active trace metadata at
`agentloop.context_transform` retain policy/version/configuration, request hashes,
summary reference, token provenance, attempted and admitted summary counts,
normalized summary usage and preparation time. Preparation time includes the
summarizer and admission work, and excludes the final provider invocation. The
default receipts contain no message or summary text. Safe error categories do
not include callback exception messages. Evidence storage is bounded; exhausting
it stops new admission explicitly. Exported receipts are copies.

Provider prefix caching remains provider-dependent. These receipts label its
outcome `unobserved`; a shorter prompt or repeated local hash proves neither a
cache hit nor a lower bill. No cache option or schema option is silently changed.

## Evaluation

The [context study](CONTEXT_STUDY.md) extends the native harness ablation
protocol with independently scored, held-out public SQL tasks. Separate context,
budget and combined arms permit direct comparisons. The report includes summary
calls in total tokens and latency, retains failed transformations, and keeps
unknown operating costs visible. A versioned quality reference identifies the
host's scorer; the runtime does not run it or claim it validates a summary.

Offline coverage includes roles, pairing, protected evidence, malformed/empty
histories, context limits, rollback, cancellation, bounded evidence, exhausted
budgets, private error text and shared usage. Synthetic runner tests separately
verify that wrong answers and summary failures cannot pass independent quality.
