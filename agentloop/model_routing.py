"""Explicit model routes and bounded, independently metered provider attempts."""

from __future__ import annotations

import inspect
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import RLock
from time import perf_counter
from types import MappingProxyType
from uuid import uuid4

from agentloop.budget_types import ResourceUsage
from agentloop.context_types import fingerprint
from agentloop.harness import (
    Decision,
    HarnessControlError,
    HarnessRun,
    Hook,
    Policy,
    _invoke_usage_reader,
)
from agentloop.routing_checks import check_route
from agentloop.routing_types import (
    ROUTING_KEY,
    ROUTING_VERSION,
    ModelBackend,
    RouteProviderError,
    RouteResponseInfo,
    RoutingConfig,
    RoutingError,
    RoutingRequest,
)
from agentloop.tracer import current_trace

_ACTIVE = ContextVar("agentloop_model_route", default=None)
_EVIDENCE_LOCK = RLock()


@dataclass(frozen=True)
class _RouteAdmission:
    config: RoutingConfig

    def __call__(self, context):
        if context.branch_id != self.config.branch_id:
            return Decision("continue", "route_not_selected")
        active = _ACTIVE.get()
        if (
            active is None
            or active["used"]
            or active["config"] != self.config
            or active["run_id"] != context.run_id
        ):
            return Decision("deny", "route_adapter_required")
        active["used"] = True
        active["attempt"]["harness_call_id"] = context.call_id
        return Decision("continue", "explicit_route_admitted")


def routing_policy(config):
    if type(config) is not RoutingConfig:
        raise ValueError("routing policy requires RoutingConfig")
    return Policy(
        config.route_id,
        config.version,
        _RouteAdmission(config),
        hooks=frozenset({Hook("model")}),
        actions=frozenset({"continue", "deny"}),
        priority=50,
        configuration=config.to_dict(),
    )


@dataclass
class _Frame:
    value: object = None
    info: RouteResponseInfo | None = None


class ModelRouter:
    """One supported synchronous or streaming model boundary per actual attempt.

    Backends are trusted host-owned SDK adapters, not dynamically loaded code.
    No router decision establishes task quality. Automatic SDK retries must be
    disabled or fully accounted for by the host adapter's usage contract.
    """

    def __init__(self, run, *, config, backends, max_records=256):
        if not isinstance(run, HarnessRun) or type(config) is not RoutingConfig:
            raise ValueError("typed run and routing configuration required")
        if (
            not isinstance(backends, (list, tuple))
            or not backends
            or any(type(item) is not ModelBackend for item in backends)
        ):
            raise ValueError("explicit typed backends required")
        registry = {item.identity: item for item in backends}
        if (
            len(registry) != len(backends)
            or not {config.original, config.target, *config.fallbacks} <= registry.keys()
            or not registry.keys() <= set(config.allowlist)
        ):
            raise ValueError("backends must be unique, complete and allowlisted")
        if type(max_records) is not int or not 1 <= max_records <= 10000:
            raise ValueError("max_records must be bounded")
        expected = routing_policy(config)
        if run.harness.config.mode != "disabled" and not any(
            policy.config_hash == expected.config_hash and type(policy.evaluate) is _RouteAdmission
            for policy in run.harness.config.policies
        ):
            raise ValueError("register the matching routing_policy before binding")
        self._run, self._config, self._backends = run, config, MappingProxyType(registry)
        self._max_records, self._policy_hash = max_records, expected.config_hash
        self._records, self._lock, self._capture_failed = {}, RLock(), False

    @property
    def run(self):
        return self._run

    @property
    def config(self):
        return self._config

    @property
    def backends(self):
        return self._backends

    @property
    def max_records(self):
        return self._max_records

    def export_evidence(self):
        with self._lock:
            return {"schema_version": ROUTING_VERSION, "records": deepcopy(self._records)}

    def _info(self, backend, value, attempt):
        try:
            info = _invoke_usage_reader(backend.inspect_response, value)
            if type(info) is not RouteResponseInfo:
                if inspect.iscoroutine(info):
                    info.close()
                raise ValueError()
        except Exception:
            attempt["inspection_error"] = "response_inspection_failed"
            if attempt["usage"] is not None:
                attempt["last_known_usage"] = attempt["usage"]
            return self._record_info(backend, RouteResponseInfo(ResourceUsage()), attempt)
        return self._record_info(backend, info, attempt)

    def _record_info(self, backend, info, attempt):
        usage = info.usage
        if usage is not None and usage.usage_id is not None:
            usage = replace(
                usage, usage_id="route:" + fingerprint([backend.identity.provider, usage.usage_id])
            )
        info = RouteResponseInfo(usage, info.reported_model)
        if info.reported_model is not None:
            attempt["provider_reported_model"] = info.reported_model
        if usage is not None:
            attempt["usage"] = usage.to_dict()
        return info

    def _invoke(self, backend, payload, attempt):
        attempt["provider_invoked"] = True
        result = backend.invoke(deepcopy(payload))
        if inspect.isawaitable(result) or hasattr(result, "__next__"):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise RoutingError("invalid_response")
        return _Frame(result, self._info(backend, result, attempt))

    def _stream_frames(self, backend, payload, attempt, state):
        source, failure = None, None
        try:
            attempt["provider_invoked"] = True
            source = backend.stream(deepcopy(payload))
            if inspect.isawaitable(source):
                close = getattr(source, "close", None)
                if callable(close):
                    close()
                raise RoutingError("unsupported_stream")
            for chunk in source:
                info = self._info(backend, chunk, attempt)
                attempt["chunks"] += 1
                yield _Frame(chunk, info)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:
                    attempt["cleanup_error"] = "stream_close_failed"
                    if isinstance(exc, RouteProviderError):
                        state["close_info"] = exc.info
                    if failure is None:
                        raise

    def _attempt(self, backend, payload, attempt, streaming, fallback):
        dispatch = (
            replace(backend.dispatch, retry_source="harness") if fallback else backend.dispatch
        )
        state = {}

        def error_usage(error):
            info = state.get("close_info")
            if info is None and isinstance(error, RouteProviderError):
                info = error.info
            return self._record_info(backend, info, attempt).usage if info is not None else None

        wrapped = self.run.wrap(
            self._stream_frames if streaming else self._invoke,
            boundary="model",
            branch_id=self.config.branch_id,
            dispatch=dispatch,
            usage_reader=lambda frame: frame.info.usage if frame.info is not None else None,
            error_usage_reader=error_usage,
        )
        binding = {
            "config": self.config,
            "run_id": self.run.run_id,
            "attempt": attempt,
            "used": False,
        }
        iterator = None
        start = perf_counter()
        try:
            if not streaming:
                token = _ACTIVE.set(binding)
                try:
                    frame = wrapped(backend, payload, attempt)
                finally:
                    _ACTIVE.reset(token)
                attempt["status"] = "completed"
                yield frame.value
                return
            iterator = wrapped(backend, payload, attempt, state)
            while True:
                token = _ACTIVE.set(binding)
                try:
                    frame = next(iterator)
                except StopIteration:
                    attempt["status"] = "completed"
                    return
                finally:
                    _ACTIVE.reset(token)
                yield frame.value
        except BaseException as exc:
            attempt["status"] = (
                "cancelled"
                if not isinstance(exc, Exception)
                else "blocked"
                if isinstance(exc, HarnessControlError)
                else "failed"
            )
            attempt["reason"] = (
                exc.category
                if isinstance(exc, RouteProviderError)
                else "harness_control"
                if isinstance(exc, HarnessControlError)
                else "cancelled"
                if not isinstance(exc, Exception)
                else "provider_error"
            )
            raise
        finally:
            try:
                if iterator is not None:
                    iterator.close()
            finally:
                attempt["latency_ms"] = (perf_counter() - start) * 1000

    def _drive(self, request, streaming):
        if type(request) is not RoutingRequest:
            raise TypeError("router requires RoutingRequest")
        payload = request.payload
        if payload["model"] != self.config.original.model:
            raise RoutingError("request_model_mismatch")
        if payload.get("stream", False) is not streaming:
            raise RoutingError("unsupported_stream")
        original = self.backends[self.config.original]
        if self.run.harness.config.mode == "disabled":
            if streaming:
                if original.stream is None:
                    raise RoutingError("unsupported_stream")
                source = original.stream(payload)
                try:
                    for chunk in source:
                        yield chunk
                finally:
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()
            else:
                yield original.invoke(payload)
            return
        if _ACTIVE.get() is not None:
            raise RoutingError("recursive_route")
        # Both adapters otherwise count the same physical call. An explicit
        # shared dispatch integration is required before composing them.
        from agentloop.context_controls import _ACTIVE as _CONTEXT_ACTIVE

        if _CONTEXT_ACTIVE.get() is not None:
            raise RoutingError("context_adapter_nesting")
        identity = "route_" + uuid4().hex
        receipt = {
            "record_id": identity,
            "route_id": self.config.route_id,
            "route_version": self.config.version,
            "route_config_hash": self._policy_hash,
            "quality_ref": self.config.quality_ref,
            "mode": self.run.harness.config.mode,
            "requested": self.config.original.to_dict(),
            "configured_target": self.config.target.to_dict(),
            "original_hash": fingerprint(payload),
            "status": "pending",
            "reason": None,
            "capability_checks": [],
            "attempts": [],
        }
        trace = current_trace()
        with self._lock, _EVIDENCE_LOCK:
            if self._capture_failed:
                raise RoutingError("invalid_trace_evidence")
            if len(self._records) >= self.max_records:
                raise RoutingError("evidence_limit")
            if trace is not None:
                envelope = trace.metadata.setdefault(
                    ROUTING_KEY, {"schema_version": ROUTING_VERSION, "records": {}}
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != ROUTING_VERSION
                    or not isinstance(envelope.get("records"), dict)
                    or len(envelope["records"]) >= 10000
                ):
                    raise RoutingError("invalid_trace_evidence")
                envelope["records"][identity] = deepcopy(receipt)
            self._records[identity] = deepcopy(receipt)
        failure = None
        started = perf_counter()
        try:
            kind = "generator" if streaming else "sync"
            if (
                kind not in self.run.harness.capabilities.execution_kinds
                or not {Hook("model"), Hook("model", "after")}
                <= self.run.harness.capabilities.hooks
            ):
                receipt["reason"] = "unsupported_harness_lifecycle"
                raise RoutingError("unsupported_route")
            target_payload = {**payload, "model": self.config.target.model}
            checks = check_route(
                self.backends[self.config.target], target_payload, streaming=streaming
            )
            receipt["capability_checks"].append({"model": self.config.target.to_dict(), **checks})
            if self.run.harness.config.mode == "shadow":
                choices, receipt["reason"] = (self.config.original,), "shadow_original"
            elif not checks["supported"]:
                if self.config.on_unsupported == "error":
                    receipt["reason"] = "unsupported_route"
                    raise RoutingError("unsupported_route")
                choices, receipt["reason"] = (self.config.original,), "unsupported_original"
            else:
                choices, receipt["reason"] = (
                    (self.config.target, *self.config.fallbacks),
                    "explicit_configuration",
                )
            for index, selected in enumerate(choices):
                backend = self.backends[selected]
                candidate = {**payload, "model": selected.model}
                if index:
                    checks = check_route(backend, candidate, streaming=streaming)
                    receipt["capability_checks"].append({"model": selected.to_dict(), **checks})
                    if not checks["supported"]:
                        receipt["reason"] = "fallback_unsupported"
                        raise RoutingError("unsupported_route")
                if streaming and backend.stream is None:
                    raise RoutingError("unsupported_stream")
                attempt = {
                    "index": index,
                    "selected": selected.to_dict(),
                    "capability_reference": backend.capabilities.reference,
                    "request_hash": fingerprint(candidate),
                    "provider_reported_model": None,
                    "provider_invoked": False,
                    "harness_call_id": None,
                    "status": "pending",
                    "reason": None,
                    "usage": None,
                    "chunks": 0,
                    "latency_ms": None,
                }
                receipt["attempts"].append(attempt)
                emitted = False
                iterator = self._attempt(backend, candidate, attempt, streaming, bool(index))
                try:
                    for value in iterator:
                        emitted = True
                        yield value
                    receipt["status"] = "completed"
                    return
                except RouteProviderError as exc:
                    if (
                        emitted
                        or exc.category not in self.config.fallback_on
                        or index + 1 == len(choices)
                        or self.run.stopped
                    ):
                        raise
                    receipt["reason"] = "configured_failure_fallback"
                finally:
                    iterator.close()
        except BaseException as exc:
            failure = exc
            receipt["status"] = (
                "cancelled"
                if not isinstance(exc, Exception)
                else "blocked"
                if isinstance(exc, HarnessControlError)
                else "failed"
            )
            receipt["error_code"] = (
                exc.reason_code
                if isinstance(exc, RoutingError)
                else exc.category
                if isinstance(exc, RouteProviderError)
                else "cancelled"
                if not isinstance(exc, Exception)
                else "harness_control"
                if isinstance(exc, HarnessControlError)
                else "provider_error"
            )
            raise
        finally:
            receipt["latency_ms"] = (perf_counter() - started) * 1000
            with self._lock, _EVIDENCE_LOCK:
                capture_error = False
                if trace is not None:
                    try:
                        current = trace.metadata[ROUTING_KEY]
                        if (
                            current is not envelope
                            or current.get("schema_version") != ROUTING_VERSION
                            or not isinstance(current["records"], dict)
                        ):
                            raise ValueError()
                        current["records"][identity] = deepcopy(receipt)
                    except (KeyError, TypeError, ValueError):
                        capture_error = self._capture_failed = True
                        receipt["capture_error"] = "invalid_trace_evidence"
                self._records[identity] = deepcopy(receipt)
            if capture_error and failure is None:
                raise RoutingError("invalid_trace_evidence")

    def __call__(self, request):
        values = self._drive(request, False)
        result = None
        try:
            for result in values:
                pass
        finally:
            values.close()
        return result

    def stream(self, request):
        yield from self._drive(request, True)
