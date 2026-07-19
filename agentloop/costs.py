"""Provider-aware model cost estimation with an explicit unknown state.

The core rule of this module: **a cost is only ever reported when it can be
justified.** A model whose pricing is not known does not silently borrow a
generic rate — it returns an ``unknown`` estimate with ``amount_usd = None``, so
downstream reports can distinguish a measured or calculated cost from a missing
one instead of presenting a fabricated number as observed.

Three kinds of cost are represented (`CostState`):

- ``provider_reported`` — the caller supplied the actual billed cost (e.g. from a
  provider usage field), which is used verbatim.
- ``calculated`` — computed from a known per-model rate in the pricing table.
- ``unknown`` — no rate is known and no reported cost was supplied; ``amount_usd``
  is ``None``.

Pricing is offline and configurable. The built-in table carries provenance (a
``source`` and an ``as_of`` date) and is a point-in-time snapshot — provider
prices change, so treat the built-ins as a convenience, not an authority, and
override them for anything that matters. Users supply their own per-model rates
without editing this package by pointing ``AGENTLOOP_PRICING_FILE`` at a JSON
file (see ``load_pricing_table``), or by passing a ``PricingTable`` explicitly.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CostState = Literal["provider_reported", "calculated", "unknown"]
CostStatus = Literal["complete", "partial", "unknown", "empty"]

# Environment variable pointing at a JSON pricing-override file. Kept here (not in
# config.py) so the cost module stays self-contained and importable without the
# rest of the package's configuration surface.
PRICING_FILE_ENV = "AGENTLOOP_PRICING_FILE"


def is_cost_evaluable(status: str | None) -> bool:
    """Return whether a cost total is complete enough for comparisons."""

    return status in {"complete", "empty"}


def format_cost_usd(
    amount_usd: float | None, status: str | None = "complete", *, decimals: int = 4
) -> str:
    """Format a cost without presenting unknown totals as exact amounts."""

    status = "complete" if status is None else status
    if (
        amount_usd is None
        or not _is_finite_number(amount_usd)
        or amount_usd < 0
        or status not in {"complete", "empty", "partial"}
    ):
        return "unavailable"
    rendered = f"${float(amount_usd):,.{decimals}f}"
    return f"{rendered} (known lower bound)" if status == "partial" else rendered


def _is_finite_number(value: Any) -> bool:
    """Return whether value is a supported finite real number.

    Values must also fit Python's floating-point calculations; extremely large
    integers are rejected instead of surfacing ``OverflowError`` later.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _is_integral_number(value: Any) -> bool:
    return _is_finite_number(value) and (
        isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    )


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing in USD per million tokens, with provenance.

    ``cached_input_usd_per_mtok`` is the (usually discounted) rate for cached
    input tokens; when ``None`` the plain input rate is used for any cached
    tokens, so a caller can always pass ``cached_input_tokens`` without needing
    every model to declare a cache rate.
    """

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    provider: str
    source: str
    as_of: str
    cached_input_usd_per_mtok: float | None = None
    # This rate is only valid up to this many input tokens; above it the entry
    # does not apply (many providers charge a higher tier for long context, e.g.
    # Gemini above 200K). ``None`` means the rate has no declared upper bound.
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, rate in (
            ("input_usd_per_mtok", self.input_usd_per_mtok),
            ("output_usd_per_mtok", self.output_usd_per_mtok),
            ("cached_input_usd_per_mtok", self.cached_input_usd_per_mtok),
        ):
            if rate is not None and (not _is_finite_number(rate) or rate < 0):
                raise ValueError(f"{name} must be a finite, non-negative number")
        if self.max_input_tokens is not None and (
            not _is_integral_number(self.max_input_tokens) or self.max_input_tokens <= 0
        ):
            raise ValueError("max_input_tokens must be a positive integer")

    def applies_to(self, input_tokens: int) -> bool:
        return self.max_input_tokens is None or input_tokens <= self.max_input_tokens

    def cost_usd(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        billable_input = max(0, input_tokens - cached_input_tokens)
        cached_rate = (
            self.cached_input_usd_per_mtok
            if self.cached_input_usd_per_mtok is not None
            else self.input_usd_per_mtok
        )
        cost = (
            (billable_input / 1_000_000) * self.input_usd_per_mtok
            + (cached_input_tokens / 1_000_000) * cached_rate
            + (output_tokens / 1_000_000) * self.output_usd_per_mtok
        )
        return round(cost, 6)


@dataclass(frozen=True)
class CostEstimate:
    """The cost of a single model call, with enough context to audit it.

    ``amount_usd`` is ``None`` exactly when ``state == "unknown"``.
    """

    state: CostState
    amount_usd: float | None
    model: str | None
    provider: str | None
    pricing_source: str | None
    pricing_as_of: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    # Populated only when ``state == "unknown"``: a short machine-usable reason
    # (e.g. ``unpriced_model``, ``context_over_threshold``,
    # ``unsupported_billing_mode``, ``invalid_metadata``) so consumers can tell
    # "no rate for this model" apart from "we refused to price this call".
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.state == "unknown") != (self.amount_usd is None):
            raise ValueError("amount_usd must be None exactly when state is unknown")
        if self.amount_usd is not None and (
            not _is_finite_number(self.amount_usd) or self.amount_usd < 0
        ):
            raise ValueError("amount_usd must be a finite, non-negative number")

    @property
    def is_known(self) -> bool:
        return self.state != "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "amount_usd": self.amount_usd,
            "model": self.model,
            "provider": self.provider,
            "pricing_source": self.pricing_source,
            "pricing_as_of": self.pricing_as_of,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "unknown_reason": self.unknown_reason,
        }


# Built-in pricing snapshot. Provenance is attached to every entry so a report
# can state where a rate came from and how current it is. These are a convenience
# for common models only — they are NOT kept up to date automatically and must be
# overridden for anything cost-sensitive (see docs/PRICING.md).
_OPENAI_SOURCE = "https://openai.com/api/pricing/"
_ANTHROPIC_SOURCE = "https://www.anthropic.com/pricing"
_GOOGLE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
_OPENAI_AS_OF = "2025-06-01"
_ANTHROPIC_AS_OF = "2025-06-01"
_GOOGLE_AS_OF = "2025-06-17"

# Gemini 2.5 charges a higher tier above 200K input tokens; the built-in flat
# rate is only correct at or below that, so above it we return unknown rather
# than under-report. A deployment that needs the long-context tier priced adds
# its own override (see docs/PRICING.md).
_GEMINI_STANDARD_CONTEXT = 200_000

_BUILTIN_PRICING: dict[str, ModelPricing] = {
    # OpenAI. The original three rates this module shipped with are preserved so
    # previously-priced traces keep the same numbers.
    "gpt-4.1": ModelPricing(2.00, 8.00, "openai", _OPENAI_SOURCE, _OPENAI_AS_OF, 0.50),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60, "openai", _OPENAI_SOURCE, _OPENAI_AS_OF, 0.10),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, "openai", _OPENAI_SOURCE, _OPENAI_AS_OF, 0.075),
    "gpt-4o": ModelPricing(2.50, 10.00, "openai", _OPENAI_SOURCE, _OPENAI_AS_OF, 1.25),
    # Anthropic.
    "claude-sonnet-4": ModelPricing(
        3.00, 15.00, "anthropic", _ANTHROPIC_SOURCE, _ANTHROPIC_AS_OF, 0.30
    ),
    "claude-opus-4": ModelPricing(
        15.00, 75.00, "anthropic", _ANTHROPIC_SOURCE, _ANTHROPIC_AS_OF, 1.50
    ),
    "claude-3-5-haiku": ModelPricing(
        0.80, 4.00, "anthropic", _ANTHROPIC_SOURCE, _ANTHROPIC_AS_OF, 0.08
    ),
    # Google. Flat rates apply only up to the standard-context threshold.
    "gemini-2.5-pro": ModelPricing(
        1.25,
        10.00,
        "google",
        _GOOGLE_SOURCE,
        _GOOGLE_AS_OF,
        max_input_tokens=_GEMINI_STANDARD_CONTEXT,
    ),
    "gemini-2.5-flash": ModelPricing(
        0.30,
        2.50,
        "google",
        _GOOGLE_SOURCE,
        _GOOGLE_AS_OF,
        max_input_tokens=_GEMINI_STANDARD_CONTEXT,
    ),
}


@dataclass
class PricingTable:
    """A resolvable set of model rates: user overrides layered over built-ins.

    Resolution normalizes a model identifier so dated snapshots and common
    prefixes map onto a base rate (e.g. ``gpt-4.1-2025-04-14`` and
    ``openai/gpt-4.1`` both resolve to ``gpt-4.1``). An override always wins over
    a built-in with the same key.
    """

    rates: dict[str, ModelPricing] = field(default_factory=dict)

    def resolve(
        self,
        model: str | None,
        provider: str | None = None,
        billing_mode: str | None = None,
    ) -> ModelPricing | None:
        """Resolve a rate, treating an explicit provider as a hard constraint.

        When a provider is specified (as the ``provider`` argument or a
        ``provider/model`` prefix), a bare-model entry only matches if its own
        ``provider`` equals it — so ``azure/gpt-4o`` or ``provider="bedrock"``
        will not silently borrow the OpenAI or Anthropic rate for a same-named
        model. A key that was itself provider-qualified (e.g. a user override
        keyed ``"azure/gpt-4o"``) is trusted as an explicit intent.

        A non-standard ``billing_mode`` (e.g. ``batch``) only matches a
        mode-qualified key; it never falls back to the standard rate.
        """
        if not model:
            return None
        effective_provider = provider or _provider_prefix(model)
        mode = _normalize_billing_mode(billing_mode)
        for candidate, provider_qualified in _resolution_candidates(model, provider, mode):
            pricing = self.rates.get(candidate)
            if pricing is None:
                continue
            if provider_qualified:
                return pricing
            if effective_provider and pricing.provider.lower() != effective_provider.lower():
                continue
            return pricing
        return None

    def with_overrides(self, overrides: dict[str, ModelPricing]) -> PricingTable:
        merged = dict(self.rates)
        merged.update(overrides)
        return PricingTable(rates=merged)


def _normalize_key(model: str) -> str:
    return model.strip().lower()


_STANDARD_BILLING_MODES = {"", "standard", "default", "sync", "on-demand"}


def _normalize_billing_mode(billing_mode: str | None) -> str | None:
    if billing_mode is None:
        return None
    mode = billing_mode.strip().lower()
    return None if mode in _STANDARD_BILLING_MODES else mode


def _provider_prefix(model: str) -> str | None:
    normalized = _normalize_key(model)
    for separator in ("/", ":"):
        if separator in normalized:
            return normalized.split(separator, 1)[0]
    return None


def _resolution_candidates(
    model: str, provider: str | None, billing_mode: str | None
) -> list[tuple[str, bool]]:
    """Ordered ``(key, provider_qualified)`` pairs for a model, most specific first.

    Handles ``provider/model`` prefixes, ``provider:model`` prefixes, and dated
    snapshot suffixes (a trailing ``-YYYY-MM-DD`` or ``-YYYYMMDD``) so a specific
    snapshot falls back to its base model rate when only the base is known. When
    ``billing_mode`` is non-standard, every key is mode-suffixed (``model#mode``)
    and the plain keys are omitted, so a non-standard mode never resolves to a
    standard rate.
    """
    normalized = _normalize_key(model)
    base: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def _add(value: str, provider_qualified: bool) -> None:
        if value and value not in seen:
            seen.add(value)
            base.append((value, provider_qualified))

    # A provider-qualified key is the most specific: try "provider/model" first.
    if provider:
        _add(f"{_normalize_key(provider)}/{normalized}", True)

    # Strip a leading "provider/" or "provider:" that came in the model string.
    bare = normalized
    for separator in ("/", ":"):
        if separator in bare:
            bare = bare.split(separator, 1)[1]
    _add(normalized, False)
    _add(bare, False)

    # Strip a trailing dated snapshot suffix so gpt-4.1-2025-04-14 -> gpt-4.1.
    for snapshot_base in (_strip_snapshot(normalized), _strip_snapshot(bare)):
        _add(snapshot_base, False)

    if billing_mode is None:
        return base
    return [(f"{key}#{billing_mode}", qualified) for key, qualified in base]


def _strip_snapshot(model: str) -> str:
    parts = model.split("-")
    # Drop a trailing YYYY-MM-DD (three parts) or YYYYMMDD (one part) date.
    if len(parts) >= 4 and _looks_like_date_parts(parts[-3:]):
        return "-".join(parts[:-3])
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
        return "-".join(parts[:-1])
    return model


def _looks_like_date_parts(parts: list[str]) -> bool:
    if len(parts) != 3:
        return False
    year, month, day = parts
    return (
        year.isdigit()
        and len(year) == 4
        and month.isdigit()
        and len(month) == 2
        and day.isdigit()
        and len(day) == 2
    )


def _pricing_from_mapping(key: str, raw: dict[str, Any]) -> ModelPricing:
    """Build a ModelPricing from a JSON/dict override entry, with clear errors."""
    try:
        input_rate = float(raw["input_usd_per_mtok"])
        output_rate = float(raw["output_usd_per_mtok"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"pricing override for {key!r} must set numeric 'input_usd_per_mtok' "
            f"and 'output_usd_per_mtok' ({exc})"
        ) from exc
    cached_raw = raw.get("cached_input_usd_per_mtok")
    max_input_raw = raw.get("max_input_tokens")
    if max_input_raw is not None and not _is_integral_number(max_input_raw):
        raise ValueError(f"pricing override for {key!r} must set an integer 'max_input_tokens'")
    return ModelPricing(
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=output_rate,
        provider=str(raw.get("provider", "custom")),
        source=str(raw.get("source", "user override")),
        as_of=str(raw.get("as_of", "user-provided")),
        cached_input_usd_per_mtok=None if cached_raw is None else float(cached_raw),
        max_input_tokens=None if max_input_raw is None else int(max_input_raw),
    )


def load_overrides_from_file(path: str | os.PathLike[str]) -> dict[str, ModelPricing]:
    """Load per-model pricing overrides from a JSON file.

    The file maps a model key (matched after normalization — a bare model name,
    ``provider/model``, or a dated snapshot are all valid keys) to an object with
    ``input_usd_per_mtok``/``output_usd_per_mtok`` and optional ``provider``,
    ``source``, ``as_of``, and ``cached_input_usd_per_mtok``. A top-level
    ``models`` wrapper object is accepted for readability.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"pricing file {os.fspath(path)} must contain a JSON object")
    entries = data.get("models") if isinstance(data.get("models"), dict) else data
    overrides: dict[str, ModelPricing] = {}
    for key, raw in entries.items():
        if not isinstance(raw, dict):
            raise ValueError(f"pricing entry for {key!r} must be an object")
        overrides[_normalize_key(key)] = _pricing_from_mapping(key, raw)
    return overrides


def load_pricing_table(
    *,
    overrides: dict[str, ModelPricing] | None = None,
    include_builtins: bool = True,
    env: dict[str, str] | None = None,
) -> PricingTable:
    """Build the effective pricing table: built-ins, then file, then explicit overrides.

    Precedence (later wins): built-in snapshot < ``AGENTLOOP_PRICING_FILE`` <
    ``overrides`` passed here. Set ``include_builtins=False`` to price *only*
    against user-provided rates (so every unlisted model is explicitly unknown).
    """
    environ = os.environ if env is None else env
    table = PricingTable(rates=dict(_BUILTIN_PRICING) if include_builtins else {})

    file_path = environ.get(PRICING_FILE_ENV)
    if file_path:
        table = table.with_overrides(load_overrides_from_file(file_path))

    if overrides:
        table = table.with_overrides({_normalize_key(k): v for k, v in overrides.items()})

    return table


def _unknown(
    model: str | None,
    provider: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    reason: str,
) -> CostEstimate:
    return CostEstimate(
        state="unknown",
        amount_usd=None,
        model=model,
        provider=provider,
        pricing_source=None,
        pricing_as_of=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        unknown_reason=reason,
    )


def estimate_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    provider: str | None = None,
    cached_input_tokens: int = 0,
    billing_mode: str | None = None,
    provider_reported_cost_usd: float | None = None,
    pricing: PricingTable | None = None,
) -> CostEstimate:
    """Estimate the cost of one model call, preferring reported > calculated > unknown.

    - If ``provider_reported_cost_usd`` is given, it's used verbatim
      (``provider_reported``) — the provider's own number always beats a
      calculation. A non-finite or negative reported cost is rejected with a
      ``ValueError`` (report construction catches this and yields ``unknown``).
    - Otherwise, if the model resolves to a known rate — under the effective
      provider, billing mode, and context threshold — the cost is
      ``calculated``.
    - Otherwise the estimate is ``unknown`` with ``amount_usd = None`` and an
      ``unknown_reason``; no generic rate is substituted.

    ``cached_input_tokens`` must satisfy ``0 <= cached <= input_tokens``.
    """
    table = pricing if pricing is not None else load_pricing_table()

    if not _is_integral_number(input_tokens) or not _is_integral_number(output_tokens):
        raise ValueError("input_tokens and output_tokens must be finite integers")
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("input_tokens and output_tokens must be non-negative")
    if not _is_integral_number(cached_input_tokens):
        raise ValueError("cached_input_tokens must be a finite integer")
    cached_input_tokens = int(cached_input_tokens)
    if not 0 <= cached_input_tokens <= input_tokens:
        raise ValueError(
            "cached_input_tokens must satisfy 0 <= cached_input_tokens <= input_tokens"
        )

    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string or None")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("provider must be a string or None")
    if billing_mode is not None and not isinstance(billing_mode, str):
        raise ValueError("billing_mode must be a string or None")

    if provider_reported_cost_usd is not None:
        if not _is_finite_number(provider_reported_cost_usd) or provider_reported_cost_usd < 0:
            raise ValueError("provider_reported_cost_usd must be a finite, non-negative number")
        resolved = table.resolve(model, provider, billing_mode)
        return CostEstimate(
            state="provider_reported",
            amount_usd=float(provider_reported_cost_usd),
            model=model,
            provider=provider or (resolved.provider if resolved else None),
            pricing_source="provider-reported",
            pricing_as_of=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )

    resolved = table.resolve(model, provider, billing_mode)
    if resolved is None:
        reason = (
            "unsupported_billing_mode"
            if _normalize_billing_mode(billing_mode) is not None
            else "unpriced_model"
        )
        return _unknown(model, provider, input_tokens, output_tokens, cached_input_tokens, reason)

    if not resolved.applies_to(input_tokens):
        # A rate exists but only for a smaller context; refuse to under-report.
        return _unknown(
            model,
            resolved.provider,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            "context_over_threshold",
        )

    return CostEstimate(
        state="calculated",
        amount_usd=resolved.cost_usd(input_tokens, output_tokens, cached_input_tokens),
        model=model,
        provider=resolved.provider,
        pricing_source=resolved.source,
        pricing_as_of=resolved.as_of,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Backward-compatible thin wrapper returning just the amount.

    **Behavior change:** this now returns ``None`` for a model with no known
    rate, instead of the old fabricated ``$1/M`` in / ``$3/M`` out default. Use
    :func:`estimate_cost` for the full provider/provenance/state context.
    """
    return estimate_cost(model, input_tokens, output_tokens).amount_usd


# Legacy export: a plain {model: {input, output}} view of the built-in rates for
# any external reader that imported MODEL_PRICES. It intentionally no longer has a
# "default" entry — the absence of a default is the whole point of this module.
MODEL_PRICES: dict[str, dict[str, float]] = {
    name: {"input": pricing.input_usd_per_mtok, "output": pricing.output_usd_per_mtok}
    for name, pricing in _BUILTIN_PRICING.items()
}
