from __future__ import annotations

import json

import pytest

from agentloop.costs import (
    MODEL_PRICES,
    ModelPricing,
    PricingTable,
    estimate_cost,
    estimate_cost_usd,
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
    estimate = estimate_cost("anything-at-all", 10, 10, provider_reported_cost_usd=0.0123)

    assert estimate.state == "provider_reported"
    assert estimate.amount_usd == 0.0123
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


def test_load_overrides_from_file_rejects_malformed_entries(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"m": {"input_usd_per_mtok": "not-a-number"}}), encoding="utf-8")

    with pytest.raises(ValueError):
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
