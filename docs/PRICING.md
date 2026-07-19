# Model pricing and cost estimation

AgentLoop turns token counts into a dollar cost for its cost, savings, value, and
pricing reports. This guide explains how that estimate is produced, how to
configure it, and — most importantly — how AgentLoop marks a cost it *cannot*
compute instead of inventing one.

## The core rule: unknown cost is visible, never fabricated

Cost estimation runs entirely offline against a pricing table. A model call falls
into one of three states:

- **`provider_reported`** — the actual billed cost was supplied on the event
  (see [Provider-reported cost](#provider-reported-cost)); it is used verbatim.
- **`calculated`** — the model resolved to a known per-model rate, so the cost is
  computed from token counts.
- **`unknown`** — no rate is known and no cost was reported. The estimate is
  `null`; **no generic fallback rate is substituted.**

This is the key behavior change from earlier versions, which assigned every
unrecognized model a fabricated `$1/M` input, `$3/M` output rate. A Claude,
Gemini, local, fine-tuned, or newly released model that isn't in the pricing
table now reports *unknown* cost rather than a plausible-looking wrong number.

## Reading a cost in a report

Every trace report carries these related fields:

- `estimated_cost_usd` — the sum of **known** cost only (calculated +
  provider-reported). A model call with unknown pricing contributes nothing here,
  so this number is never inflated by a fabricated rate. It is still a plain
  float for backward compatibility.
- `cost_status` — a first-class completeness flag so a consumer reading the flat
  number knows whether to trust it as exact:
  - `complete` — every model call had a known cost; `estimated_cost_usd` is exact.
  - `empty` — no model calls, so there is no cost.
  - `partial` — some calls priced, some not; `estimated_cost_usd` is a **lower
    bound**.
  - `unknown` — no call could be priced.

  `optimization_plan`, the value report, and the replay report each carry
  `cost_status` too, and the replay report renders its cost delta/percentages as
  `unavailable` (JSON `null`) whenever either side is not `complete` — so
  lower-bound arithmetic is never presented as a valid comparison.
- `cost_breakdown` — the full picture:

  ```json
  {
    "known_cost_usd": 0.0235,
    "calculated_usd": 0.0035,
    "provider_reported_usd": 0.02,
    "priced_model_call_count": 2,
    "unavailable_model_call_count": 1,
    "has_unknown_cost": true,
    "pricing_sources": ["https://openai.com/api/pricing/"],
    "pricing_as_of": ["2025-06-01"],
    "unknown_models": ["some-local-model"],
    "model_calls": [ ... per-call estimates ... ]
  }
  ```

  When `has_unknown_cost` is `true`, treat `estimated_cost_usd` as a **lower
  bound** — real cost is at least that, plus whatever the unpriced calls cost.

## Pricing changes over time

The built-in pricing table is a **point-in-time snapshot** (its `as_of` date is
reported in `pricing_as_of`). Provider prices change, new models ship, and old
snapshots get repriced. The built-ins are a convenience for common models, **not
an authority** — for anything cost-sensitive, supply your own rates so you
control both the numbers and their `as_of` provenance.

## Supplying your own rates

You do not edit the package to price a model. There are two mechanisms.

### A pricing file (recommended)

Point the `AGENTLOOP_PRICING_FILE` environment variable at a JSON file:

```bash
export AGENTLOOP_PRICING_FILE=/etc/agentloop/pricing.json
```

```json
{
  "models": {
    "my-local-llama": {
      "input_usd_per_mtok": 0.0,
      "output_usd_per_mtok": 0.0,
      "provider": "self-hosted",
      "source": "internal — GPU amortized separately",
      "as_of": "2026-01-15"
    },
    "gpt-4o": {
      "input_usd_per_mtok": 2.0,
      "output_usd_per_mtok": 8.0,
      "source": "negotiated enterprise rate",
      "as_of": "2026-01-01"
    },
    "claude-sonnet-4-20250514": {
      "input_usd_per_mtok": 3.0,
      "output_usd_per_mtok": 15.0,
      "cached_input_usd_per_mtok": 0.3
    }
  }
}
```

- Keys are matched after normalization: a bare model name, a `provider/model`
  string, a specific dated snapshot, or a `model#mode` billing-mode key are all
  valid.
- `input_usd_per_mtok` and `output_usd_per_mtok` are required; `provider`,
  `source`, `as_of`, `cached_input_usd_per_mtok`, and `max_input_tokens` (a
  context ceiling above which the entry does not apply) are optional.
- A file entry **overrides** the built-in rate for the same model.
- The top-level `models` wrapper is optional — a flat `{ "model": { ... } }`
  object works too.

### Programmatic overrides

`agentloop.costs.load_pricing_table(overrides=..., include_builtins=...)` builds a
`PricingTable` you can pass to `estimate_cost(...)`. Set `include_builtins=False`
to price **only** against your own rates, so every model you haven't listed is
explicitly unknown — useful when you want a hard guarantee that no built-in
snapshot silently prices something.

## Model resolution

A model identifier is resolved most-specific-first, so you can price at whatever
granularity you need:

1. `provider/model` (when a provider is supplied)
2. the exact identifier
3. the identifier with any leading `provider/` or `provider:` stripped
4. the identifier with a trailing dated snapshot removed
   (`gpt-4.1-2025-04-14` → `gpt-4.1`, `gpt-4.1-20250414` → `gpt-4.1`)

So a dated snapshot automatically inherits its base model's rate unless you price
the snapshot explicitly.

### Provider is a hard constraint

When a provider is given — as the `provider` argument or a `provider/model`
prefix — a bare-model rate only matches if its own provider is the same one.
`azure/gpt-4o` will **not** borrow the OpenAI `gpt-4o` rate, and
`provider="bedrock"` will not borrow the first-party Anthropic rate for a
same-named Claude model; both resolve to **unknown** unless you add a rate for
that provider. To price a provider's hosting explicitly, add an override — either
keyed `provider/model` (e.g. `"azure/gpt-4o"`, which is trusted as-is) or with a
matching `provider` field.

### Context thresholds

A rate can declare a `max_input_tokens` above which it does not apply (many
providers charge a higher tier for long context). The built-in Gemini 2.5 rates
apply only at or below 200K input tokens; above that they resolve to **unknown**
(`unknown_reason: context_over_threshold`) rather than under-report. Add an
override with a higher `max_input_tokens` (or none) to price the long-context
tier yourself.

### Billing modes

`estimate_cost(..., billing_mode=...)` — or an event's `metadata.billing_mode` —
selects a mode-specific rate. A non-standard mode (e.g. `batch`, `priority`)
**only** matches a mode-qualified key like `"gpt-4o#batch"`; it never falls back
to the standard rate, so an unpriced mode is **unknown**
(`unknown_reason: unsupported_billing_mode`). `standard`/`default`/`sync`/
`on-demand` (and no mode) use the ordinary rate.

## Provider-reported cost

If your integration knows the real billed cost of a call, attach it to the
event's `metadata` and AgentLoop will use it directly (state
`provider_reported`), in preference to any calculated rate:

```python
with trace_model_call(
    "answer",
    model="gpt-4o",
    input_tokens=1200,
    output_tokens=300,
    metadata={"provider_reported_cost_usd": 0.0141},
):
    ...
```

Related `metadata` keys read by cost estimation:

- `provider` — disambiguates the model against the pricing table.
- `cached_input_tokens` — billed at the model's cached-input rate when it has one.
- `provider_reported_cost_usd` (or `cost_usd`) — the actual billed cost.

These ride in the free-form event `metadata`, so adding them never breaks an
existing serialized trace.

## Replay gates and unknown cost

Replay/CI compares baseline and candidate cost. When **either** side has an
unknown model cost, the cost gates cannot be computed from the partial numbers,
so they are marked **indeterminate** rather than silently compared against
coerced zeros:

- By default (no cost threshold required), an indeterminate cost gate is
  reported but does **not** fail the replay — a latency-only optimization that
  happens to use an unpriced model is not blocked. The report's
  `gates.cost_evaluable` is `false` and `gates.indeterminate` lists the affected
  gates, so the gap is visible.
- If you **require** a cost improvement (`min_cost_improvement_pct > 0`) and the
  cost is unknown, the `cost_improvement` gate **fails** — AgentLoop will not
  claim an improvement it cannot verify.

To get evaluable cost gates for models outside the built-in table, price them via
`AGENTLOOP_PRICING_FILE`.
