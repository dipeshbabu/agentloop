"""Payload-free resource declarations shared by dispatch wrappers and budgets."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

TOKEN_PROVENANCE = frozenset(
    {
        "provider",
        "tokenizer",
        "user_supplied",
        "estimated_words",
        "legacy",
        "unspecified",
        "unavailable",
    }
)
COST_PROVENANCE = frozenset(
    {"provider_reported", "user_reported", "calculated", "estimated", "unavailable"}
)
RETRY_SOURCES = frozenset({"framework", "provider", "harness"})
EXACT_TOKENS = frozenset({"provider", "tokenizer", "user_supplied"})
COUNT_KEYS = ("model_calls", "tool_calls", "iterations", "retries")
METRIC_KEYS = (*COUNT_KEYS, "tokens", "cost_usd")
_MONEY_TEXT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class BudgetValidationError(ValueError):
    """A resource declaration is invalid; errors never include response payloads."""


def count(value: Any, name: str, *, optional: bool = True) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise BudgetValidationError(f"{name} must be a nonnegative integer")
    return value


def number(value: Any, name: str, *, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    try:
        if type(value) not in {int, float} or value < 0 or not math.isfinite(float(value)):
            raise ValueError
    except (ValueError, OverflowError):
        raise BudgetValidationError(f"{name} must be finite and nonnegative") from None
    return float(value)


def amount(value: int | float) -> Fraction:
    """Accumulate declared decimal amounts exactly, avoiding float drift at a cap."""
    number(value, "cost_usd", optional=False)
    return Fraction(str(value))


def money_text(value: Fraction) -> str:
    """Render exact decimal-input arithmetic as portable JSON decimal text."""
    with localcontext() as context:
        context.prec = len(str(value.numerator)) + len(str(value.denominator)) + 5
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _boolean(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise BudgetValidationError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Reservation:
    """Caller-declared conservative bounds, not proof of a provider billing cap."""

    tokens: int | None = None
    cost_usd: float | None = None
    provenance: str = "unavailable"
    pricing_known: bool = False
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        count(self.tokens, "reserved tokens")
        number(self.cost_usd, "reserved cost")
        if not isinstance(self.provenance, str) or self.provenance not in {
            "upper_bound",
            "estimated",
            "unavailable",
        }:
            raise BudgetValidationError("unsupported reservation provenance")
        _boolean(self.pricing_known, "pricing_known")
        if not isinstance(self.evidence_refs, (tuple, list)) or any(
            not isinstance(item, str) or not item for item in self.evidence_refs
        ):
            raise BudgetValidationError("reservation evidence_refs must contain opaque identifiers")
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))

    @property
    def tokens_known(self) -> bool:
        return self.tokens is not None and self.provenance == "upper_bound"

    @property
    def cost_known(self) -> bool:
        return self.cost_usd is not None and self.provenance == "upper_bound" and self.pricing_known

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceUsage:
    """One exclusive usage snapshot; streams must explicitly identify final usage."""

    tokens: int | None = None
    cost_usd: float | None = None
    token_provenance: str | None = None
    cost_provenance: str | None = None
    pricing_known: bool = False
    complete: bool = False
    exclusive: bool = True
    usage_id: str | None = None

    def __post_init__(self) -> None:
        count(self.tokens, "usage tokens")
        number(self.cost_usd, "usage cost")
        if self.token_provenance is not None and (
            not isinstance(self.token_provenance, str)
            or self.token_provenance not in TOKEN_PROVENANCE
        ):
            raise BudgetValidationError("unsupported token provenance")
        if self.cost_provenance is not None and (
            not isinstance(self.cost_provenance, str) or self.cost_provenance not in COST_PROVENANCE
        ):
            raise BudgetValidationError("unsupported cost provenance")
        for name in ("pricing_known", "complete", "exclusive"):
            _boolean(getattr(self, name), name)
        if self.usage_id is not None and (not isinstance(self.usage_id, str) or not self.usage_id):
            raise BudgetValidationError("usage_id must be an opaque nonempty string")

    @property
    def tokens_known(self) -> bool:
        return (
            self.complete
            and self.exclusive
            and self.tokens is not None
            and self.token_provenance in EXACT_TOKENS
        )

    @property
    def cost_known(self) -> bool:
        return (
            self.complete
            and self.exclusive
            and self.cost_usd is not None
            and (
                self.cost_provenance in {"provider_reported", "user_reported"}
                or (
                    self.cost_provenance == "calculated"
                    and self.pricing_known
                    and self.tokens_known
                )
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchOptions:
    """Safe per-binding admission hints; no call arguments or output content."""

    reservation: Reservation | None = None
    retry_source: str | None = None

    def __post_init__(self) -> None:
        if self.reservation is not None and type(self.reservation) is not Reservation:
            raise BudgetValidationError("reservation must be a Reservation")
        if self.retry_source is not None and (
            not isinstance(self.retry_source, str) or self.retry_source not in RETRY_SOURCES
        ):
            raise BudgetValidationError("unsupported retry source")


@dataclass(frozen=True)
class BudgetSnapshot:
    """A closed numeric schema prevents arbitrary payloads entering decision evidence."""

    _json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BudgetSnapshot:
        fields = {
            "schema_version",
            "scope",
            "hard_spend_cap",
            "deadline_enforcement",
            "metered_boundaries",
            "limits",
            "committed",
            "in_flight",
            "refunded",
            "unknown_usage",
            "total_usage",
            "retry_sources",
            "status_codes",
            "soft_exceeded",
            "overshoot",
            "deadline_remaining_s",
            "last_usage",
            "usage_error",
            "reservation",
            "budget_scope_id",
            "spend_enforcement",
            "token_enforcement",
            "possible_spend_overshoot",
            "unknown_usage_policy",
            "soft_fraction",
        }
        if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "1.0":
            raise BudgetValidationError("invalid budget snapshot fields")
        if (
            value["scope"] != "wrapped_dispatch"
            or value["hard_spend_cap"] is not False
            or value["deadline_enforcement"] != "cooperative_admission"
        ):
            raise BudgetValidationError("unsupported budget guarantee")
        if not isinstance(value["budget_scope_id"], str) or not value["budget_scope_id"]:
            raise BudgetValidationError("budget scope must have an opaque identity")
        if not isinstance(value["metered_boundaries"], list) or any(
            not isinstance(item, str) or item not in {"model", "tool"}
            for item in value["metered_boundaries"]
        ):
            raise BudgetValidationError("invalid metered boundaries")
        groups = {
            "limits": (set(COUNT_KEYS) | {"tokens", "cost_usd", "deadline_at"}, True),
            "committed": (
                set(COUNT_KEYS) | {"metered_calls", "tokens_known", "cost_known_usd"},
                False,
            ),
            "in_flight": (
                set(COUNT_KEYS)
                | {
                    "metered_calls",
                    "tokens_reserved",
                    "cost_reserved_usd",
                    "unknown_token_reservations",
                    "unknown_cost_reservations",
                },
                False,
            ),
            "refunded": (set(COUNT_KEYS) | {"tokens_bound", "cost_bound_usd"}, False),
            "unknown_usage": ({"calls", "tokens", "cost", "tokens_held", "cost_held_usd"}, False),
            "total_usage": ({"tokens", "cost_usd"}, True),
            "overshoot": ({"tokens", "cost_usd"}, True),
            "retry_sources": (set(RETRY_SOURCES), False),
        }
        for group, (keys, optional) in groups.items():
            items = value[group]
            if not isinstance(items, dict) or set(items) != keys:
                raise BudgetValidationError("invalid budget counter group")
            for key, item in items.items():
                if optional and item is None:
                    continue
                if key.endswith("usd"):
                    if not isinstance(item, str) or not _MONEY_TEXT.fullmatch(item):
                        raise BudgetValidationError("budget costs must be nonnegative decimal text")
                elif key == "deadline_at":
                    number(item, key, optional=optional)
                else:
                    count(item, key, optional=optional)
        for key, limit in (("spend_enforcement", "cost_usd"), ("token_enforcement", "tokens")):
            if value[key] not in ("not_configured", "shadow", "best_effort"):
                raise BudgetValidationError("unsupported resource enforcement claim")
            if (value[key] == "not_configured") != (value["limits"][limit] is None):
                raise BudgetValidationError("resource enforcement must match configured limits")
        expected_overshoot = None if value["limits"]["cost_usd"] is None else True
        if value["possible_spend_overshoot"] is not expected_overshoot:
            raise BudgetValidationError("the Python adapter cannot exclude spend overshoot")
        if not isinstance(value["unknown_usage_policy"], str) or value[
            "unknown_usage_policy"
        ] not in {"deny", "escalate", "monitor_only"}:
            raise BudgetValidationError("invalid unknown-usage policy")
        soft = number(value["soft_fraction"], "soft_fraction")
        if soft is not None and soft > 1:
            raise BudgetValidationError("soft_fraction must not exceed one")
        number(value["deadline_remaining_s"], "deadline remaining")
        codes = {
            "ok",
            "not_admitted",
            "unknown_tokens",
            "unknown_cost",
            "bound_exceeded",
            "deadline_expired",
            "duplicate_usage",
            "usage_conflict",
            "usage_collector_error",
            "clock_invalid",
        }
        codes |= {f"limit_{key}" for key in METRIC_KEYS}
        if not isinstance(value["status_codes"], list) or any(
            not isinstance(code, str) or code not in codes for code in value["status_codes"]
        ):
            raise BudgetValidationError("invalid budget status")
        if not isinstance(value["soft_exceeded"], list) or any(
            key not in METRIC_KEYS for key in value["soft_exceeded"]
        ):
            raise BudgetValidationError("invalid soft threshold metric")
        if value["usage_error"] is not None and (
            not isinstance(value["usage_error"], str)
            or value["usage_error"]
            not in {"collector_error", "collector_cancelled", "invalid_usage"}
        ):
            raise BudgetValidationError("invalid usage diagnostic")
        try:
            if value["last_usage"] is not None:
                ResourceUsage(**value["last_usage"])
            if value["reservation"] is not None:
                Reservation(**value["reservation"])
            serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise BudgetValidationError("invalid budget usage or reservation") from None
        return cls(serialized)
