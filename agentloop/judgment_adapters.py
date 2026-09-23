"""Replaceable local prediction and typed-service bridges for judgment contract 1.0."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Callable

from agentloop.judgment_types import (
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentUncertainty,
    canonical,
)


def _callback(value, name):
    if (
        not callable(value)
        or inspect.iscoroutinefunction(value)
        or inspect.isasyncgenfunction(value)
    ):
        raise ValueError(f"{name} must be a synchronous callable")


@dataclass(frozen=True)
class LocalPredictorJudge:
    """Adapt a local scalar predictor without assuming its model framework or labels.

    The predictor accepts a JudgmentRequest and keyword timeout_s. The session
    validates its output against the request's type/domain. Usage is caller-owned
    metadata; no zero token/cost claim is inferred from the word 'local'.
    """

    identity: JudgeIdentity
    predict: Callable
    usage: JudgeUsage = JudgeUsage()

    def __post_init__(self):
        if type(self.identity) is not JudgeIdentity or type(self.usage) is not JudgeUsage:
            raise ValueError("predictor requires a typed identity and usage")
        _callback(self.predict, "predict")

    def judge(self, request, *, timeout_s=None):
        value = self.predict(request, timeout_s=timeout_s)
        if type(value) is JudgmentAnswer:
            return value
        if inspect.iscoroutine(value) or inspect.isgenerator(value):
            value.close()
            raise ValueError("predictors must return a scalar, not deferred work")
        return JudgmentAnswer(value, usage=self.usage)


def parse_typed_answer(response):
    """Accept a finite structured response, never free-form prose or extra fields."""
    if not isinstance(response, dict) or set(response) - {
        "value",
        "status",
        "reason",
        "usage",
        "uncertainty",
    }:
        raise ValueError("service response must be a typed answer object")
    encoded = canonical(response)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("service answer exceeds the 64 KiB contract limit")
    owned = json.loads(encoded)
    return JudgmentAnswer(
        value=owned.get("value"),
        status=owned.get("status", "known"),
        reason=owned.get("reason"),
        usage=JudgeUsage(**owned.get("usage", {})),
        uncertainty=JudgmentUncertainty(**owned.get("uncertainty", {})),
    )


@dataclass(frozen=True)
class TypedServiceJudge:
    """Adapt an optional host-owned transport using AgentLoop's structured schema.

    Credentials and client SDKs belong to the host transport. This adapter never
    reads environment secrets, imports a provider, or opens a network connection
    itself. A configured transport may perform remote work only during explicit
    session execution. The transport owns connection/response byte limits and
    cooperative cancellation before returning a decoded JSON object.
    """

    identity: JudgeIdentity
    transport: Callable | None = None
    credentials_required: bool = True
    credentials_available: bool = False

    def __post_init__(self):
        if type(self.identity) is not JudgeIdentity:
            raise ValueError("service requires JudgeIdentity")
        if (
            type(self.credentials_required) is not bool
            or type(self.credentials_available) is not bool
        ):
            raise ValueError("credential availability must be a boolean declaration")
        if self.transport is not None:
            _callback(self.transport, "transport")

    @property
    def availability(self):
        if self.transport is None:
            return "not_configured"
        if self.credentials_required and not self.credentials_available:
            return "missing_credentials"
        return "ready"

    def judge(self, request, *, timeout_s=None):
        if self.availability != "ready":
            return JudgmentAnswer(
                status="unknown",
                reason="abstained",
                usage=JudgeUsage(0, 0, 0, "not_dispatched", "not_dispatched"),
            )
        response = self.transport(request.to_dict(), timeout_s=timeout_s)
        if inspect.iscoroutine(response) or inspect.isgenerator(response):
            response.close()
            raise ValueError("transport must return a decoded JSON answer")
        return parse_typed_answer(response)
