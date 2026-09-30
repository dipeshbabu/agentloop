"""Owned requests and explicit provider/model capability declarations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agentloop.budget_types import DispatchOptions, ResourceUsage
from agentloop.context_types import ContextTokenCount, canonical, identifier, synchronous

ROUTING_VERSION = "1.0"
ROUTING_KEY = "agentloop.model_routing"
FAILURE_KINDS = frozenset({"rate_limit", "unavailable", "timeout", "provider_error"})


def model_name(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("model identity must be a bounded nonempty string")


class RoutingError(ValueError):
    """A fixed failure category without provider payloads."""

    def __init__(self, reason):
        allowed = {
            "invalid_request",
            "unsupported_route",
            "request_model_mismatch",
            "invalid_response",
            "inspection_failed",
            "evidence_limit",
            "invalid_trace_evidence",
            "recursive_route",
            "unsupported_stream",
            "context_adapter_nesting",
        }
        self.reason_code = (
            reason if isinstance(reason, str) and reason in allowed else "invalid_request"
        )
        super().__init__(f"Model routing failed: {self.reason_code}")


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    model: str

    def __post_init__(self):
        identifier(self.provider, "provider")
        model_name(self.model)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ModelCapabilities:
    reference: str
    context_window: int
    parameters: tuple[str, ...] = ("model", "messages", "max_tokens")
    output_modes: tuple[str, ...] = ("text",)
    modalities: tuple[str, ...] = ("text",)
    tool_calling: bool = False
    streaming: bool = False
    adapter: str = "json_chat_v1"

    def __post_init__(self):
        identifier(self.reference, "capability reference")
        if type(self.context_window) is not int or self.context_window < 1:
            raise ValueError("context_window must be positive")
        for name, allowed in (
            ("output_modes", {"text", "json_object", "json_schema"}),
            ("modalities", {"text", "image", "audio"}),
            ("parameters", None),
        ):
            values = getattr(self, name)
            if (
                not isinstance(values, (tuple, list, set, frozenset))
                or not values
                or len(values) > 64
                or any(
                    not isinstance(value, str) or not value or len(value) > 128 for value in values
                )
            ):
                raise ValueError("capabilities require bounded string collections")
            if allowed is not None and not set(values) <= allowed:
                raise ValueError("unsupported capability declaration")
            object.__setattr__(self, name, tuple(sorted(set(values))))
        if (
            type(self.tool_calling) is not bool
            or type(self.streaming) is not bool
            or not isinstance(self.adapter, str)
        ):
            raise ValueError("invalid adapter capability")

    def to_dict(self):
        return json.loads(canonical(asdict(self)))


@dataclass(frozen=True, init=False)
class RoutingRequest:
    _json: str = field(repr=False)

    def __init__(self, payload):
        try:
            if not isinstance(payload, dict):
                raise ValueError()
            canonical(payload)
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            if len(encoded.encode("utf-8")) > 1_000_000:
                raise ValueError()
            model_name(payload.get("model"))
        except (ValueError, TypeError, RecursionError):
            raise RoutingError("invalid_request") from None
        object.__setattr__(self, "_json", encoded)

    @property
    def payload(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class RouteResponseInfo:
    usage: ResourceUsage | None = None
    reported_model: str | None = None

    def __post_init__(self):
        if self.usage is not None and type(self.usage) is not ResourceUsage:
            raise ValueError("response usage must be normalized ResourceUsage")
        if self.reported_model is not None:
            model_name(self.reported_model)


class RouteProviderError(Exception):
    """Host adapter classification permitting only configured failure fallbacks.

    An SDK adapter may raise this from its original SDK exception. Usage is an
    exclusive snapshot when available; absent billing is never inferred as zero.
    """

    def __init__(self, category, *, info=None):
        if (
            not isinstance(category, str)
            or category not in FAILURE_KINDS
            or (info is not None and type(info) is not RouteResponseInfo)
        ):
            raise ValueError("invalid provider failure declaration")
        self.category = category
        self.info = info or RouteResponseInfo()
        super().__init__(f"Routed provider failure: {category}")


@dataclass(frozen=True)
class ModelBackend:
    identity: ModelIdentity
    capabilities: ModelCapabilities
    invoke: Callable[[dict], Any] = field(repr=False, compare=False)
    token_counter: Callable[[dict], ContextTokenCount] = field(repr=False, compare=False)
    inspect_response: Callable[[Any], RouteResponseInfo] = field(repr=False, compare=False)
    stream: Callable[[dict], Any] | None = field(default=None, repr=False, compare=False)
    dispatch: DispatchOptions = field(default_factory=DispatchOptions)

    def __post_init__(self):
        if (
            type(self.identity) is not ModelIdentity
            or type(self.capabilities) is not ModelCapabilities
            or type(self.dispatch) is not DispatchOptions
        ):
            raise ValueError("backend requires typed identity, capabilities and dispatch")
        if any(
            not synchronous(callback)
            for callback in (self.invoke, self.token_counter, self.inspect_response)
        ):
            raise ValueError("backend callbacks must be synchronous")
        if self.stream is not None and not synchronous(self.stream):
            raise ValueError("stream factory must be synchronous and return an SDK iterator")
        if self.capabilities.streaming and self.stream is None:
            raise ValueError("streaming capability requires an explicit SDK stream factory")


@dataclass(frozen=True)
class RoutingConfig:
    route_id: str
    version: str
    branch_id: str
    quality_ref: str
    original: ModelIdentity
    target: ModelIdentity
    allowlist: tuple[ModelIdentity, ...]
    fallbacks: tuple[ModelIdentity, ...] = ()
    fallback_on: tuple[str, ...] = ()
    on_unsupported: str = "error"
    allow_cross_provider: bool = False

    def __post_init__(self):
        for name in ("route_id", "version", "branch_id", "quality_ref"):
            identifier(getattr(self, name), name)
        if type(self.original) is not ModelIdentity or type(self.target) is not ModelIdentity:
            raise ValueError("route requires typed original and target models")
        for name, maximum in (("allowlist", 32), ("fallbacks", 2)):
            values = getattr(self, name)
            if (
                not isinstance(values, (list, tuple))
                or len(values) > maximum
                or any(type(item) is not ModelIdentity for item in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError("route models must be unique and bounded")
            object.__setattr__(self, name, tuple(values))
        if (
            not {self.original, self.target, *self.fallbacks} <= set(self.allowlist)
            or self.target in self.fallbacks
        ):
            raise ValueError(
                "every route endpoint must be allowlisted; fallback cannot repeat target"
            )
        if not isinstance(self.fallback_on, (list, tuple)) or any(
            not isinstance(value, str) or value not in FAILURE_KINDS for value in self.fallback_on
        ):
            raise ValueError("fallback requires explicit supported failure categories")
        object.__setattr__(self, "fallback_on", tuple(sorted(set(self.fallback_on))))
        if (
            self.on_unsupported not in {"error", "original"}
            or type(self.allow_cross_provider) is not bool
        ):
            raise ValueError("invalid unsupported/cross-provider policy")
        if not self.allow_cross_provider and any(
            item.provider != self.original.provider for item in (self.target, *self.fallbacks)
        ):
            raise ValueError("cross-provider routing requires explicit configuration")

    def to_dict(self):
        return json.loads(canonical(asdict(self)))
