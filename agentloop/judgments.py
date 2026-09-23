"""Explicit offline judgment execution, bounded caching and portable receipts."""

from __future__ import annotations

import inspect
import json
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Callable
from uuid import uuid4

from agentloop.events import AgentEvent, utc_now_iso
from agentloop.judgment_types import (
    JUDGMENT_KEY,
    JUDGMENT_VERSION,
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentBackend,
    JudgmentEvidence,
    JudgmentRequest,
    JudgmentSpec,
    JudgmentUncertainty,
    canonical,
    fingerprint,
    finite,
)
from agentloop.operations import OPERATION_KINDS, operation_kind

_NO_USAGE = JudgeUsage(0, 0, 0.0, "not_dispatched", "not_dispatched")
_FAILURES = {"disabled", "timeout", "error", "invalid_result"}


def _copy(value):
    return json.loads(canonical(value))


def _event_hash(event):
    # Use the importer normalization so an integer duration and its native JSON
    # round trip have the same fingerprint. Bodies are hashed, never forwarded.
    source = AgentEvent.from_dict(event.to_dict()).to_dict()
    source["metadata"] = {
        key: value
        for key, value in source["metadata"].items()
        if key not in {"otel_span_id", "otel_trace_id"}
    }
    return fingerprint(source)


def _evidence(event, summary, hashes):
    if event.event_id not in hashes:
        hashes[event.event_id] = _event_hash(event)
    kind = operation_kind(event)
    return JudgmentEvidence(
        event.event_id,
        hashes[event.event_id],
        kind if kind in OPERATION_KINDS else "unknown",
        event.status if event.status in {"ok", "error"} else "unknown",
        float(event.duration_ms),
        summary,
    )


def judgment_request(trace, spec: JudgmentSpec, span_ids, *, summaries=None) -> JudgmentRequest:
    """Select references/observations; only explicitly supplied summaries carry text."""
    if not isinstance(span_ids, (list, tuple)) or not span_ids:
        raise ValueError("span_ids must be a nonempty list or tuple")
    if any(not isinstance(item, str) for item in span_ids) or len(set(span_ids)) != len(span_ids):
        raise ValueError("span_ids must contain unique strings")
    summaries = {} if summaries is None else summaries
    if not isinstance(summaries, dict) or set(summaries) - set(span_ids):
        raise ValueError("summaries must map selected span IDs to explicitly supplied text")
    events = {event.event_id: event for event in trace.events}
    if len(events) != len(trace.events) or set(span_ids) - events.keys():
        raise ValueError("selected span IDs must resolve uniquely in the trace")
    hashes = {}
    evidence = [_evidence(events[span_id], summaries.get(span_id), hashes) for span_id in span_ids]
    return JudgmentRequest(trace.run_id, spec, tuple(evidence))


@dataclass(frozen=True)
class LocalCallbackJudge:
    """Trusted synchronous callback; no imports, provider client or worker threads."""

    identity: JudgeIdentity
    callback: Callable

    def __post_init__(self):
        if type(self.identity) is not JudgeIdentity or not callable(self.callback):
            raise ValueError("local judge requires an identity and callable")
        if inspect.iscoroutinefunction(self.callback) or inspect.isasyncgenfunction(self.callback):
            raise ValueError("local callbacks must be synchronous")

    def judge(self, request, *, timeout_s=None):
        return self.callback(request, timeout_s=timeout_s)


class JudgmentSession:
    """One backend and a bounded session-local LRU; execution requires enabled=True.

    Not thread-safe. Use one session per analysis worker. No cache is shared
    across backend instances or restored from untrusted trace metadata.
    """

    def __init__(self, backend: JudgmentBackend, *, cache_size: int = 128):
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")
        if type(backend.identity) is not JudgeIdentity or not callable(backend.judge):
            raise ValueError("backend must implement JudgmentBackend")
        self._backend = backend
        self._cache_size = cache_size
        self._cache = OrderedDict()

    def clear_cache(self):
        self._cache.clear()

    def evaluate(self, request: JudgmentRequest, *, enabled=False, timeout_s=None):
        if type(request) is not JudgmentRequest or type(enabled) is not bool:
            raise ValueError("evaluate requires a typed request and boolean enabled flag")
        if timeout_s is not None and (not finite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be finite and positive")
        identity = self._backend.identity
        if type(identity) is not JudgeIdentity:
            raise ValueError("backend identity must be JudgeIdentity")
        request_data, judge = request.to_dict(), asdict(identity)
        key = fingerprint(
            {"schema_version": JUDGMENT_VERSION, "request": request_data, "judge": judge}
        )
        started = time.perf_counter()
        hit = enabled and key in self._cache
        if hit:
            evaluation = _copy(self._cache[key])
            self._cache.move_to_end(key)
        else:
            evaluation = self._evaluate(request, enabled, timeout_s)
            if enabled and self._backend.identity != identity:
                evaluation.update(status="invalid_result", value=None, reason="identity_changed")
            if evaluation["status"] == "known" and self._cache_size:
                self._cache[key] = _copy(evaluation)
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        record = {
            "schema_version": JUDGMENT_VERSION,
            "evidence_kind": "semantic_judgment",
            "request": request_data,
            "judge": judge,
            "cache_key": key,
            "evaluation": evaluation,
            "invocation": {
                "id": uuid4().hex,
                "cache_hit": hit,
                "dispatched": enabled and not hit,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "usage": asdict(_NO_USAGE) if hit or not enabled else evaluation["usage"],
            },
        }
        record["record_hash"] = fingerprint(record)
        return _copy(record)

    def _evaluate(self, request, enabled, timeout_s):
        started = time.perf_counter()
        result = {
            "id": uuid4().hex,
            "evaluated_at": utc_now_iso(),
            "status": "disabled",
            "value": None,
            "reason": "disabled",
            "usage": asdict(_NO_USAGE if not enabled else JudgeUsage()),
            "uncertainty": asdict(JudgmentUncertainty()),
        }
        if enabled:
            try:
                answer = self._backend.judge(request, timeout_s=timeout_s)
                if type(answer) is not JudgmentAnswer:
                    if inspect.iscoroutine(answer) or inspect.isgenerator(answer):
                        answer.close()
                    result.update(status="invalid_result", reason="invalid_result")
                else:
                    result.update(asdict(answer))
                    if answer.status == "known" and not request.spec.accepts(answer.value):
                        result.update(status="invalid_result", value=None, reason="invalid_result")
            except TimeoutError:
                result.update(status="timeout", reason="timeout")
            except Exception:
                # Neither exception text nor arbitrary class names leave the callback.
                result.update(status="error", reason="error")
        elapsed = time.perf_counter() - started
        if enabled and timeout_s is not None and elapsed > timeout_s:
            result.update(status="timeout", value=None, reason="timeout")
        result["latency_ms"] = elapsed * 1000
        return result


def validate_judgment_record(record):
    """Validate portable integrity and typed semantics, not a judge's truthfulness."""
    value = _copy(record)
    try:
        digest = value.pop("record_hash")
        if digest != fingerprint(value) or value["schema_version"] != JUDGMENT_VERSION:
            raise ValueError("unsupported or altered judgment record")
        if value["evidence_kind"] != "semantic_judgment":
            raise ValueError("invalid evidence kind")
        request = JudgmentRequest.from_dict(value["request"])
        judge = JudgeIdentity(**value["judge"])
        key = fingerprint(
            {
                "schema_version": JUDGMENT_VERSION,
                "request": request.to_dict(),
                "judge": asdict(judge),
            }
        )
        if value["cache_key"] != key:
            raise ValueError("judgment cache identity mismatch")
        result, invocation = value["evaluation"], value["invocation"]
        usage = JudgeUsage(**result["usage"])
        uncertainty = JudgmentUncertainty(**result["uncertainty"])
        if result["status"] in {"known", "unknown"}:
            answer = JudgmentAnswer(
                result["value"], result["status"], result["reason"], usage, uncertainty
            )
            if answer.status == "known" and not request.spec.accepts(answer.value):
                raise ValueError("answer violates typed bounds")
        elif (
            result["status"] not in _FAILURES
            or result["value"] is not None
            or result["reason"] not in _FAILURES | {"identity_changed"}
        ):
            raise ValueError("invalid failure result")
        for part in (result, invocation):
            if (
                not isinstance(part["id"], str)
                or not part["id"]
                or not finite(part["latency_ms"])
                or part["latency_ms"] < 0
            ):
                raise ValueError("invalid execution receipt")
        if not isinstance(result["evaluated_at"], str):
            raise ValueError("invalid evaluation timestamp")
        if type(invocation["cache_hit"]) is not bool or type(invocation["dispatched"]) is not bool:
            raise ValueError("invalid cache receipt")
        incurred = JudgeUsage(**invocation["usage"])
        if invocation["cache_hit"]:
            if invocation["dispatched"] or result["status"] != "known":
                raise ValueError("only successful undispatched evaluations can be cached")
        if not invocation["dispatched"]:
            if incurred != _NO_USAGE or (
                not invocation["cache_hit"] and result["status"] != "disabled"
            ):
                raise ValueError("invalid undispatched receipt")
        elif incurred != usage or result["status"] == "disabled":
            raise ValueError("invalid dispatched receipt")
        value["record_hash"] = digest
        return value
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid judgment record") from None


def _bound(trace, record, *, events=None, hashes=None):
    request = JudgmentRequest.from_dict(record["request"])
    if request.run_id != trace.run_id:
        return False
    if events is None:
        events = {event.event_id: event for event in trace.events}
    if len(events) != len(trace.events):
        return False
    hashes = {} if hashes is None else hashes
    return all(
        item.span_id in events and _evidence(events[item.span_id], item.summary, hashes) == item
        for item in request.evidence
    )


def attach_judgment(trace, record):
    """Attach a validated receipt without replacing host metadata or prior results."""
    owned = validate_judgment_record(record)
    if not _bound(trace, owned):
        raise ValueError("judgment evidence does not match the trace")
    existing = trace.metadata.get(JUDGMENT_KEY)
    if JUDGMENT_KEY not in trace.metadata:
        existing = {"schema_version": JUDGMENT_VERSION, "records": []}
    elif read_judgments(trace)["status"] in {"invalid", "unsupported"}:
        raise ValueError("existing judgment evidence is invalid or unsupported")
    existing = _copy(existing)
    for previous in existing["records"]:
        if previous["invocation"]["id"] == owned["invocation"]["id"]:
            if previous != owned:
                raise ValueError("conflicting invocation receipt")
            return
    existing["records"].append(owned)
    trace.metadata[JUDGMENT_KEY] = existing


def read_judgments(trace):
    """Read saved evidence only. Stale or failed judgments never become booleans."""
    metadata = getattr(trace, "metadata", {})
    envelope = metadata.get(JUDGMENT_KEY)
    if JUDGMENT_KEY not in metadata:
        return None
    empty = {"status": "invalid", "records": [], "known_cost_usd": None, "cost_complete": False}
    if not isinstance(envelope, dict):
        return empty
    if envelope.get("schema_version") != JUDGMENT_VERSION:
        return {**empty, "status": "unsupported"}
    try:
        if not isinstance(envelope.get("records"), list):
            return empty
        records, identities, known_cost, complete_cost = [], set(), 0.0, True
        events = {event.event_id: event for event in trace.events}
        hashes = {}
        for raw in envelope["records"]:
            record = validate_judgment_record(raw)
            identity = record["invocation"]["id"]
            if identity in identities:
                return empty
            identities.add(identity)
            try:
                bound = _bound(trace, record, events=events, hashes=hashes)
            except ValueError:
                bound = False
            result = record["evaluation"]
            record["effective_status"] = result["status"] if bound else "stale"
            record["effective_value"] = (
                result["value"] if bound and result["status"] == "known" else None
            )
            cost = record["invocation"]["usage"]["cost_usd"]
            if cost is None:
                complete_cost = False
            else:
                known_cost += cost
            records.append(record)
        return {
            "status": "complete"
            if records and all(item["effective_status"] == "known" for item in records)
            else "incomplete",
            "records": records,
            "known_cost_usd": known_cost if finite(known_cost) else None,
            "cost_complete": complete_cost and finite(known_cost),
            "cost_scope": "offline_judge_invocations_only",
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return empty
