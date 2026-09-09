"""Token-count provenance: where a token number came from, and how exact it is.

The core rule of this module mirrors :mod:`agentloop.costs`: **a measurement is
only ever presented as exact when it can be justified.** AgentLoop stores a
whitespace word-count fallback in the same ``input_tokens`` / ``output_tokens``
fields that carry provider-reported usage, so without provenance a trace can show
an exact-looking token count — and an exact-looking dollar amount derived from it
— that actually came from ``len(text.split())``.

Six provenance values are represented (`TokenProvenance`), recorded per event:

- ``provider``       — the provider reported usage (e.g. an OpenAI ``usage`` object).
- ``tokenizer``      — counted with a real tokenizer for the target model.
- ``user_supplied``  — explicit counts passed by the calling application.
- ``estimated_words`` — the whitespace word-count fallback; an approximation.
- ``unavailable``    — no counts were available and none could be estimated.
- ``unspecified``    — the trace predates this field, so provenance is unknown.

The first three are *exact-grade*: they are counts of tokens rather than a proxy
for them. ``estimated_words`` is explicitly an approximation. Aggregating those
grades over a trace's model calls yields a `TokenStatus`, which is what reports,
cost breakdowns, and replay gates consult. See ``docs/TRACE_SCHEMA.md`` for the
serialized contract and ``docs/RESEARCH.md`` for which measurements are suitable
to report as experimental results.
"""

from __future__ import annotations

from typing import Any, Literal

TokenProvenance = Literal[
    "provider",
    "tokenizer",
    "user_supplied",
    "estimated_words",
    "unavailable",
    "unspecified",
]
TokenStatus = Literal["exact", "estimated", "partial", "unavailable", "unspecified", "empty"]

PROVIDER = "provider"
TOKENIZER = "tokenizer"
USER_SUPPLIED = "user_supplied"
ESTIMATED_WORDS = "estimated_words"
UNAVAILABLE = "unavailable"
UNSPECIFIED = "unspecified"

#: Provenance values a producer may record. ``unspecified`` is a *read* result
#: for traces written before the field existed, never something to write.
WRITABLE_PROVENANCE = frozenset({PROVIDER, TOKENIZER, USER_SUPPLIED, ESTIMATED_WORDS, UNAVAILABLE})

#: Provenance values that count tokens rather than approximate them.
EXACT_PROVENANCE = frozenset({PROVIDER, TOKENIZER, USER_SUPPLIED})

# Exactness grades, in the order the aggregate status resolves them.
_EXACT = "exact"
_ESTIMATED = "estimated"
_UNAVAILABLE = "unavailable"
_UNSPECIFIED = "unspecified"


class TokenProvenanceError(ValueError):
    """A producer tried to record a token provenance AgentLoop does not define."""


def validate_provenance(value: Any) -> str:
    """Return ``value`` if a producer may record it, else raise.

    Producers (the tracer and the integrations) validate strictly so a typo fails
    fast at the point it is introduced. The *deserialization* path deliberately
    does not: see :func:`provenance_grade` for why unrecognized values read back
    as ``unspecified`` instead of rejecting a trace from a newer minor version.
    """

    if value not in WRITABLE_PROVENANCE:
        raise TokenProvenanceError(
            f"unknown token provenance {value!r}; expected one of "
            f"{', '.join(sorted(WRITABLE_PROVENANCE))}"
        )
    return str(value)


def provenance_grade(value: Any) -> str:
    """Return the exactness grade for one event's provenance value.

    A missing value (an event from a trace written before this field existed) and
    any value this build does not recognize both grade as ``unspecified``. Being
    lenient here is what keeps the schema's MINOR forward-compatibility promise:
    a trace written by a newer AgentLoop that adds a provenance value stays
    readable, and its unknown value is simply not claimed to be exact.
    """

    if value is None:
        return _UNSPECIFIED
    if value in EXACT_PROVENANCE:
        return _EXACT
    if value == ESTIMATED_WORDS:
        return _ESTIMATED
    if value == UNAVAILABLE:
        return _UNAVAILABLE
    return _UNSPECIFIED


def token_status(model_events: list[Any]) -> str:
    """Summarize how exact a trace's token totals are.

    - ``empty``       — no model calls, so there is no token count to qualify.
    - ``exact``       — every model call counted tokens (provider/tokenizer/user).
    - ``partial``     — some model calls counted tokens and some did not.
    - ``estimated``   — no exact counts, and at least one is a word approximation.
    - ``unavailable`` — every model call recorded that it had no counts at all.
    - ``unspecified`` — provenance is unknown for every model call (legacy trace).

    Consumers should treat anything other than ``exact``/``empty`` as "do not
    present these token totals, or a cost derived from them, as an exact
    measurement."
    """

    grades = {provenance_grade(getattr(event, "token_provenance", None)) for event in model_events}
    if not grades:
        return "empty"
    if grades == {_EXACT}:
        return "exact"
    if _EXACT in grades:
        return "partial"
    if _ESTIMATED in grades:
        return "estimated"
    if grades == {_UNAVAILABLE}:
        return "unavailable"
    return "unspecified"


def provenance_counts(model_events: list[Any]) -> dict[str, int]:
    """Count model calls per recorded provenance value, for report transparency."""

    counts: dict[str, int] = {}
    for event in model_events:
        value = getattr(event, "token_provenance", None)
        key = str(value) if value is not None else UNSPECIFIED
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def is_token_basis_exact(status: str | None) -> bool:
    """Return whether token totals are exact enough to report as measurements."""

    return status in {"exact", "empty"}


def is_token_basis_evaluable(status: str | None) -> bool:
    """Return whether token totals may back a cost comparison or gate.

    Stricter than nothing, looser than :func:`is_token_basis_exact`: a trace with
    *unknown* provenance (``unspecified``) still gates, because every trace
    written before this field existed would otherwise turn every configured cost
    gate indeterminate on upgrade. A trace that positively reports a word
    estimate does not gate — there the producer told us the number is a proxy.
    See ``docs/TRACE_SCHEMA.md`` for the compatibility path.
    """

    return status in {"exact", "empty", "unspecified", None}


def describe_token_status(status: str | None) -> str:
    """Return a short human explanation of a token status, for reports and CLI."""

    return {
        "exact": "counted tokens (provider, tokenizer, or caller-supplied)",
        "partial": "mixed: some model calls counted tokens, some did not",
        "estimated": "approximated from whitespace word counts, not counted tokens",
        "unavailable": "no token counts were available",
        "unspecified": "unknown: trace predates token provenance",
        "empty": "no model calls",
    }.get(str(status), "unknown token basis")
