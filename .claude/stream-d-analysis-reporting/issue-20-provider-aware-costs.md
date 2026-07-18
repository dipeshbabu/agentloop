# Issue #20 — Make model cost estimates provider-aware and explicit for unknown models

- **Priority:** P1 · **Effort:** S–M · **Labels:** enhancement, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/20

## Problem

The cost calculator recognizes **three** exact model names; every other model silently uses a
generic `$1/M` input and `$3/M` output rate with no "estimated/unsupported" marker. That
fabricated rate flows into regression gates, recommendation priority, modeled monthly value,
and suggested pricing — indistinguishable from measured cost.

## Key files

- `agentloop/costs.py:3-8` — small static table + `default` rate.
- `agentloop/costs.py:12` — `estimate_cost_usd()` selects the default for any unknown model.
- `agentloop/metrics.py:32` — aggregates into `estimated_cost_usd`.
- Vercel/OTLP + generic integrations ingest models from any provider without provider/pricing metadata.

## Approach

1. **Provider-aware pricing interface**, usable offline, with explicit user overrides
   (per-model/provider rates supplied without editing package code — e.g. config/env/file).
2. **Record pricing provenance:** source/version or an "as of" date on the pricing data.
3. **Make unknown pricing visible.** Unknown models return an explicit unknown/partial state
   or warning — never a synthetic rate presented as observed cost.
4. **Distinguish cost kinds in reports:** provider-reported vs. calculated vs. unavailable.
5. **Extension path** for cached input, batch/priority modes, provider-specific billable units.
6. **Define replay-gate behavior for unknown cost** (don't coerce to zero/default).
7. Document that pricing changes over time and how to configure/update it.

## Acceptance criteria (from the issue)

- [ ] Events/report config can identify provider and exact model/snapshot.
- [ ] Pricing data records source/version or an "as of" date.
- [ ] Users can supply per-model/provider rates without changing package code.
- [ ] Unknown models return an explicit unknown/partial state or warning; no synthetic rate presented as observed.
- [ ] Cached input, batch/priority modes, and provider-specific billable units have an extension path.
- [ ] Reports distinguish provider-reported, calculated, and unavailable cost.
- [ ] Replay gates define how unknown costs behave instead of coercing to zero/default.
- [ ] Tests cover aliases, dated snapshots, multiple providers, local models, overrides, and unknown models.
- [ ] Documentation explains pricing changes over time and how to update/configure it.

## Testing

- `tests/test_metrics.py` / new cost tests: known model, alias, dated snapshot, multiple
  providers, local/fine-tuned model, user override, and unknown model → explicit unknown state.

## Compatibility / risk

- Reports gain an explicit "unknown/estimated" state — a shape change consumers may read;
  document it. Numbers for previously-defaulted models will change (become unknown), which is
  the intended correction.
