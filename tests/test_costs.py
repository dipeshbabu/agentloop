from __future__ import annotations

import json

import pytest

from agentloop.costs import (
    MODEL_PRICES,
    ModelPricing,
    PricingTable,
    estimate_cost,
    estimate_cost_usd,
    format_cost_usd,
    load_overrides_from_file,
    load_pricing_table,
)


def test_known_model_is_calculated_with_provenance() -> None:
    estimate = estimate_cost("gpt-4.1-mini", 1000, 200)

    assert estimate.state == "calculated"
    assert estimate.amount_usd is not None and estimate.amount_usd > 0
    assert estimate.provider == "openai"
    assert estimate.pricing_source
    assert estimate.pricing_as_of == "2025-06-01"
    assert estimate.is_known


def test_dated_snapshot_falls_back_to_base_model_rate() -> None:
    base = estimate_cost("gpt-4.1", 1000, 100)
    snapshot = estimate_cost("gpt-4.1-2025-04-14", 1000, 100)
    compact = estimate_cost("gpt-4.1-20250414", 1000, 100)

    assert snapshot.state == "calculated"
    assert snapshot.amount_usd == base.amount_usd
    assert compact.amount_usd == base.amount_usd


def test_provider_prefixed_and_provider_argument_both_resolve() -> None:
    prefixed = estimate_cost("openai/gpt-4o-mini", 1000, 100)
    with_arg = estimate_cost("gpt-4o-mini", 1000, 100, provider="openai")

    assert prefixed.state == "calculated"
    assert with_arg.state == "calculated"
    assert prefixed.amount_usd == with_arg.amount_usd


def test_multiple_providers_are_priced_independently() -> None:
    openai = estimate_cost("gpt-4o", 1000, 1000)
    anthropic = estimate_cost("claude-sonnet-4", 1000, 1000)
    google = estimate_cost("gemini-2.5-flash", 1000, 1000)

    assert {openai.provider, anthropic.provider, google.provider} == {
        "openai",
        "anthropic",
        "google",
    }
    # distinct rate tables -> distinct amounts
    assert len({openai.amount_usd, anthropic.amount_usd, google.amount_usd}) == 3


def test_local_or_finetuned_model_is_unknown_not_defaulted() -> None:
    local = estimate_cost("llama-3.1-70b-local", 1000, 1000)
    finetuned = estimate_cost("ft:gpt-4.1:acme:custom", 1000, 1000)

    assert local.state == "unknown"
    assert local.amount_usd is None
    # a fine-tune ARN-style id does not silently borrow the base model's rate
    assert finetuned.state == "unknown"
    assert finetuned.amount_usd is None


def test_unknown_model_returns_explicit_state_and_no_synthetic_rate() -> None:
    estimate = estimate_cost("some-brand-new-model", 1_000_000, 1_000_000)

    assert estimate.state == "unknown"
    assert estimate.amount_usd is None
    assert not estimate.is_known


def test_provider_reported_cost_is_used_verbatim() -> None:
    reported = 0.0123456789
    estimate = estimate_cost("anything-at-all", 10, 10, provider_reported_cost_usd=reported)

    assert estimate.state == "provider_reported"
    assert estimate.amount_usd == reported
    assert estimate.pricing_source == "provider-reported"


def test_cached_input_tokens_are_discounted_when_a_cache_rate_exists() -> None:
    full = estimate_cost("gpt-4.1-mini", 1000, 0)
    cached = estimate_cost("gpt-4.1-mini", 1000, 0, cached_input_tokens=1000)

    assert cached.amount_usd is not None and full.amount_usd is not None
    assert cached.amount_usd < full.amount_usd


def test_user_override_prices_a_model_without_editing_the_package() -> None:
    overrides = {
        "my-local-model": ModelPricing(
            input_usd_per_mtok=0.05,
            output_usd_per_mtok=0.10,
            provider="self-hosted",
            source="ops spreadsheet",
            as_of="2026-01-01",
        )
    }
    table = load_pricing_table(overrides=overrides)
    estimate = estimate_cost("my-local-model", 1_000_000, 1_000_000, pricing=table)

    assert estimate.state == "calculated"
    assert estimate.amount_usd == pytest.approx(0.15)
    assert estimate.provider == "self-hosted"
    assert estimate.pricing_as_of == "2026-01-01"


def test_override_wins_over_builtin_for_the_same_model() -> None:
    table = load_pricing_table(
        overrides={"gpt-4o": ModelPricing(1.0, 1.0, "openai", "internal deal", "2026-01-01")}
    )
    estimate = estimate_cost("gpt-4o", 1_000_000, 1_000_000, pricing=table)

    assert estimate.amount_usd == pytest.approx(2.0)
    assert estimate.pricing_source == "internal deal"


def test_pricing_file_env_var_is_loaded(tmp_path, monkeypatch) -> None:
    pricing_file = tmp_path / "prices.json"
    pricing_file.write_text(
        json.dumps(
            {
                "models": {
                    "fancy-model": {
                        "input_usd_per_mtok": 1.0,
                        "output_usd_per_mtok": 2.0,
                        "provider": "acme",
                        "as_of": "2026-02-02",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTLOOP_PRICING_FILE", str(pricing_file))

    estimate = estimate_cost("fancy-model", 1_000_000, 0)

    assert estimate.state == "calculated"
    assert estimate.amount_usd == pytest.approx(1.0)
    assert estimate.provider == "acme"


def test_include_builtins_false_makes_every_unlisted_model_unknown() -> None:
    table = load_pricing_table(include_builtins=False)

    assert estimate_cost("gpt-4o", 1000, 1000, pricing=table).state == "unknown"


@pytest.mark.parametrize("bad_rate", ["not-a-number", float("nan"), float("inf"), -1.0, True])
def test_load_overrides_from_file_rejects_malformed_entries(tmp_path, bad_rate) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "m": {
                    "input_usd_per_mtok": bad_rate,
                    "output_usd_per_mtok": 1.0,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_overrides_from_file(bad)


@pytest.mark.parametrize("field", ["output_usd_per_mtok", "cached_input_usd_per_mtok"])
def test_load_overrides_from_file_rejects_boolean_rates(tmp_path, field) -> None:
    bad = tmp_path / f"bad-{field}.json"
    bad.write_text(
        json.dumps(
            {
                "m": {
                    "input_usd_per_mtok": 1.0,
                    "output_usd_per_mtok": 1.0,
                    field: True,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="boolean"):
        load_overrides_from_file(bad)


def test_legacy_estimate_cost_usd_returns_none_for_unknown_model() -> None:
    assert estimate_cost_usd("gpt-4.1-mini", 100, 20) is not None
    assert estimate_cost_usd("totally-unknown-model", 100, 20) is None


def test_legacy_model_prices_export_has_no_default_entry() -> None:
    assert "default" not in MODEL_PRICES
    assert "gpt-4.1" in MODEL_PRICES


def test_pricing_table_resolve_returns_none_for_missing_model() -> None:
    table = PricingTable(rates={})
    assert table.resolve("anything") is None
    assert table.resolve(None) is None


# --- provider enforced as a hard constraint (review: costs.py provider) ---


def test_provider_prefix_does_not_borrow_another_providers_rate() -> None:
    # azure/gpt-4o must not silently resolve to the OpenAI gpt-4o rate
    estimate = estimate_cost("azure/gpt-4o", 1000, 100)
    assert estimate.state == "unknown"
    assert estimate.unknown_reason == "unpriced_model"


def test_provider_argument_mismatch_returns_unknown() -> None:
    # bedrock-hosted Claude must not resolve to the Anthropic first-party rate
    estimate = estimate_cost("claude-sonnet-4", 1000, 100, provider="bedrock")
    assert estimate.state == "unknown"


def test_matching_provider_still_resolves() -> None:
    assert estimate_cost("gpt-4o", 1000, 100, provider="openai").state == "calculated"
    assert estimate_cost("openai/gpt-4o", 1000, 100).state == "calculated"
    # a provider-qualified override key is trusted regardless of its provider field
    table = load_pricing_table(
        overrides={"azure/gpt-4o": ModelPricing(2.5, 10.0, "azure", "deal", "2026-01-01")}
    )
    assert estimate_cost("azure/gpt-4o", 1000, 100, pricing=table).state == "calculated"


def test_no_provider_specified_matches_by_model_name_as_before() -> None:
    assert estimate_cost("gpt-4o", 1000, 100).state == "calculated"


# --- context threshold + billing modes (review: costs.py Gemini/flat) ---


def test_context_over_threshold_is_unknown_not_underreported() -> None:
    small = estimate_cost("gemini-2.5-pro", 1000, 100)
    large = estimate_cost("gemini-2.5-pro", 300_000, 100)
    assert small.state == "calculated"
    assert large.state == "unknown"
    assert large.unknown_reason == "context_over_threshold"


def test_google_stable_model_pricing_provenance_matches_ga_release() -> None:
    estimate = estimate_cost("gemini-2.5-flash", 1000, 100)
    assert estimate.pricing_as_of == "2025-06-17"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_usd_per_mtok", float("nan")),
        ("output_usd_per_mtok", float("inf")),
        ("cached_input_usd_per_mtok", -0.01),
        ("max_input_tokens", 0),
        ("max_input_tokens", -1),
        ("max_input_tokens", 1.5),
    ],
)
def test_model_pricing_rejects_invalid_invariants(field, value) -> None:
    values = {
        "input_usd_per_mtok": 1.0,
        "output_usd_per_mtok": 2.0,
        "provider": "test",
        "source": "test",
        "as_of": "2026-01-01",
    }
    values[field] = value
    with pytest.raises(ValueError):
        ModelPricing(**values)


def test_model_pricing_rejects_none_for_required_rates() -> None:
    with pytest.raises(ValueError):
        ModelPricing(None, 1.0, "test", "test", "2026-01-01")
    with pytest.raises(ValueError):
        ModelPricing(1.0, None, "test", "test", "2026-01-01")


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "cached_input_tokens"),
    [
        (-1, 0, 0),
        (1, -1, 0),
        (1.5, 0, 0),
        (1, float("nan"), 0),
        (100, 0, -1),
        (100, 0, 101),
        (100, 0, float("inf")),
    ],
)
def test_model_pricing_cost_rejects_invalid_token_counts(
    input_tokens, output_tokens, cached_input_tokens
) -> None:
    pricing = ModelPricing(1.0, 2.0, "test", "test", "2026-01-01")
    with pytest.raises(ValueError):
        pricing.cost_usd(input_tokens, output_tokens, cached_input_tokens)


def test_model_pricing_cost_accepts_cached_boundary() -> None:
    pricing = ModelPricing(1.0, 2.0, "test", "test", "2026-01-01", 0.5)
    assert pricing.cost_usd(100, 0, 100) == pytest.approx(0.00005)


def test_non_standard_billing_mode_requires_a_mode_specific_rate() -> None:
    # no batch rate for gpt-4o -> unknown, not the standard rate
    unpriced = estimate_cost("gpt-4o", 1000, 100, billing_mode="batch")
    assert unpriced.state == "unknown"
    assert unpriced.unknown_reason == "unsupported_billing_mode"
    # a batch override keyed model#batch is used only for batch mode
    table = load_pricing_table(
        overrides={"gpt-4o#batch": ModelPricing(1.25, 5.0, "openai", "batch", "2025-06-01")}
    )
    assert estimate_cost("gpt-4o", 1000, 100, billing_mode="batch", pricing=table).state == (
        "calculated"
    )
    # standard mode still uses the standard rate
    assert estimate_cost("gpt-4o", 1000, 100, pricing=table).state == "calculated"


# --- input validation (review: metrics.py untrusted metadata) ---


def test_estimate_cost_rejects_non_finite_or_negative_reported_cost() -> None:
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            estimate_cost("gpt-4o", 100, 10, provider_reported_cost_usd=bad)


def test_estimate_cost_rejects_cached_tokens_out_of_range() -> None:
    with pytest.raises(ValueError):
        estimate_cost("gpt-4o", 100, 10, cached_input_tokens=200)
    with pytest.raises(ValueError):
        estimate_cost("gpt-4o", 100, 10, cached_input_tokens=-5)


@pytest.mark.parametrize(
    "bad",
    [1.5, float("nan"), float("inf"), pytest.param(10**10_000, id="oversized-integer")],
)
def test_estimate_cost_rejects_non_integral_token_counts(bad) -> None:
    with pytest.raises(ValueError):
        estimate_cost("gpt-4o", bad, 10)
    with pytest.raises(ValueError):
        estimate_cost("gpt-4o", 100, 10, cached_input_tokens=bad)


def test_unknown_reason_is_populated_only_when_unknown() -> None:
    assert estimate_cost("gpt-4o", 100, 10).unknown_reason is None
    assert estimate_cost("nope", 100, 10).unknown_reason == "unpriced_model"


def test_cost_formatter_fails_closed_for_invalid_status() -> None:
    assert format_cost_usd(1.0, "invalid") == "unavailable"
    assert format_cost_usd(float("nan")) == "unavailable"
    assert format_cost_usd(-1.0) == "unavailable"
