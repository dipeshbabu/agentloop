"""Explicit, owned request contracts for optional tool-result summaries."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Callable

from agentloop.budget_types import DispatchOptions, ResourceUsage

CONTEXT_VERSION = "1.0"
CONTEXT_KEY = "agentloop.context_transform"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
EXACT_COUNTS = {"provider", "tokenizer", "user_supplied"}
_REASONS = frozenset(
    "invalid_history invalid_role invalid_tool_calls unsupported_content "
    "unpaired_tool_result missing_tool_result invalid_tool_reference ambiguous_tool_call "
    "token_counter_failed invalid_token_count unsupported_adapter streaming_not_supported "
    "unknown_tool_selection summary_count_limit summary_failed invalid_summary "
    "token_counter_changed context_limit_unverified tokens_not_reduced "
    "unexpected_streaming_result recursive_context_transform evidence_limit "
    "invalid_trace_evidence invalid_json_request invalid_request request_too_large "
    "invalid_evidence_selection transformation_error".split()
)


class ContextTransformError(ValueError):
    """A bounded failure category; never include request or summary content."""

    def __init__(self, reason):
        if not isinstance(reason, str) or reason not in _REASONS:
            reason = "transformation_error"
        self.reason_code = reason
        super().__init__(f"Context transformation failed: {reason}")


def canonical(value):
    try:

        def validate_keys(item):
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise TypeError("non-string JSON key")
                for child in item.values():
                    validate_keys(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    validate_keys(child)

        validate_keys(value)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (ValueError, TypeError, RecursionError):
        raise ContextTransformError("invalid_json_request") from None


def fingerprint(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def synchronous(function):
    return callable(function) and not any(
        check(function) or check(getattr(function, "__call__", None))
        for check in (
            inspect.iscoroutinefunction,
            inspect.isgeneratorfunction,
            inspect.isasyncgenfunction,
        )
    )


@dataclass(frozen=True, init=False)
class ContextRequest:
    """Own a JSON request; every payload export is a fresh deep copy.

    Only explicitly optional tool results can change. All other messages and
    all provider options are preserved. Protected tool results take precedence.
    """

    _json: str = field(repr=False)
    optional_tool_results: tuple[str, ...]
    protected_tool_results: tuple[str, ...]

    def __init__(self, payload, *, optional_tool_results=(), protected_tool_results=()):
        if not isinstance(payload, dict):
            raise ContextTransformError("invalid_request")
        canonical(payload)  # Validate finite JSON without changing option ordering.
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ContextTransformError("request_too_large")
        for name, values in (
            ("optional_tool_results", optional_tool_results),
            ("protected_tool_results", protected_tool_results),
        ):
            if (
                not isinstance(values, (tuple, list, set, frozenset))
                or len(values) > 256
                or any(
                    not isinstance(value, str) or not value or len(value) > 256 for value in values
                )
            ):
                raise ContextTransformError("invalid_evidence_selection")
            object.__setattr__(self, name, tuple(sorted(set(values))))
        object.__setattr__(self, "_json", encoded)

    @property
    def payload(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class ContextPolicyConfig:
    policy_id: str
    version: str
    branch_id: str
    quality_ref: str
    max_summary_chars: int = 512
    max_summaries: int = 1
    max_input_tokens: int | None = None
    on_invalid: str = "error"

    def __post_init__(self):
        for name in ("policy_id", "version", "branch_id", "quality_ref"):
            identifier(getattr(self, name), name)
        for name, maximum in (("max_summary_chars", 8000), ("max_summaries", 8)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be a positive bounded integer")
        if self.max_input_tokens is not None and (
            type(self.max_input_tokens) is not int or self.max_input_tokens < 1
        ):
            raise ValueError("max_input_tokens must be positive when configured")
        if self.on_invalid not in {"error", "original"}:
            raise ValueError("on_invalid must be error or original")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ContextAdapter:
    """Host declaration for the first supported synchronous text-chat boundary."""

    name: str
    before_model: bool = True
    format: str = "text_tool_messages_v1"

    def __post_init__(self):
        identifier(self.name, "adapter name")
        if type(self.before_model) is not bool or not isinstance(self.format, str):
            raise ValueError("invalid adapter declaration")


@dataclass(frozen=True)
class ContextTokenCount:
    value: int | None = None
    provenance: str = "unavailable"
    reference: str | None = None

    def __post_init__(self):
        if self.provenance not in EXACT_COUNTS | {"estimated_words", "unavailable"}:
            raise ValueError("unsupported token provenance")
        if self.value is not None and (type(self.value) is not int or self.value < 0):
            raise ValueError("token count must be nonnegative")
        if self.provenance == "unavailable" and self.value is not None:
            raise ValueError("unavailable token count cannot claim a value")
        if self.provenance != "unavailable" and (self.value is None or self.reference is None):
            raise ValueError("available token counts require a versioned reference")
        if self.reference is not None:
            identifier(self.reference, "tokenizer reference")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SummaryInput:
    """Untrusted tool data; the callback must preserve this trust classification."""

    content: str = field(repr=False)
    max_chars: int
    source_hash: str
    trust_class: str = field(default="tool", init=False)


@dataclass(frozen=True)
class SummaryResult:
    text: str = field(repr=False)
    usage: ResourceUsage | None = None

    def __post_init__(self):
        if not isinstance(self.text, str) or (
            self.usage is not None and type(self.usage) is not ResourceUsage
        ):
            raise ValueError("summary requires text and normalized usage")


@dataclass(frozen=True)
class SummaryBackend:
    reference: str
    summarize: Callable[[SummaryInput], SummaryResult] = field(repr=False, compare=False)
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)

    def __post_init__(self):
        identifier(self.reference, "summarizer reference")
        if not synchronous(self.summarize) or type(self.dispatch) is not DispatchOptions:
            raise ValueError("summarizer must be a synchronous declared dispatch")
