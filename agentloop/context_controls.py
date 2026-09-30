"""Opt-in tool-result summaries after supported before-model admission hooks."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import perf_counter
from uuid import uuid4

from agentloop.budget_types import DispatchOptions
from agentloop.context_types import (
    CONTEXT_KEY,
    CONTEXT_VERSION,
    EXACT_COUNTS,
    ContextAdapter,
    ContextPolicyConfig,
    ContextRequest,
    ContextTokenCount,
    ContextTransformError,
    SummaryBackend,
    SummaryInput,
    SummaryResult,
    fingerprint,
    synchronous,
)
from agentloop.harness import Decision, HarnessControlError, HarnessRun, Hook, Policy
from agentloop.tracer import current_trace

_ACTIVE: ContextVar[dict | None] = ContextVar("agentloop_context_preparation", default=None)
_EVIDENCE_LOCK = RLock()


@dataclass(frozen=True)
class _ContextAdmission:
    config: ContextPolicyConfig

    def __call__(self, context):
        if context.branch_id != self.config.branch_id:
            return Decision("continue", "context_not_selected")
        active = _ACTIVE.get()
        if (
            active is None
            or active["used"]
            or active["config"] != self.config
            or active["run_id"] != context.run_id
        ):
            return Decision("deny", "context_adapter_required")
        # One-shot binding: nested generic calls cannot borrow this admission,
        # even when a caller reuses a run label across different HarnessRuns.
        active["used"] = True
        active["receipt"]["harness_call_id"] = context.call_id
        return Decision("continue", "context_preparation_admitted")


def context_policy(config: ContextPolicyConfig):
    if type(config) is not ContextPolicyConfig:
        raise ValueError("context policy requires ContextPolicyConfig")
    return Policy(
        config.policy_id,
        config.version,
        _ContextAdmission(config),
        hooks=frozenset({Hook("model")}),
        actions=frozenset({"continue", "deny"}),
        priority=50,
        configuration={"transformation": "summarize_optional_tool_results", **config.to_dict()},
    )


def _tool_results(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 512:
        raise ContextTransformError("invalid_history")
    pending, declared, results = set(), set(), {}
    for index, message in enumerate(messages):
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or message["role"]
            not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }
        ):
            raise ContextTransformError("invalid_role")
        role = message["role"]
        calls = message.get("tool_calls")
        if role != "assistant" and calls is not None:
            raise ContextTransformError("invalid_tool_calls")
        content = message.get("content")
        if not isinstance(content, str) and not (role == "assistant" and content is None and calls):
            raise ContextTransformError("unsupported_content")
        if role == "tool":
            identity = message.get("tool_call_id")
            if not isinstance(identity, str) or identity not in pending or identity in results:
                raise ContextTransformError("unpaired_tool_result")
            pending.remove(identity)
            results[identity] = index
        else:
            if pending:
                raise ContextTransformError("missing_tool_result")
            if message.get("tool_call_id") is not None:
                raise ContextTransformError("invalid_tool_reference")
            if calls is not None:
                if not isinstance(calls, list) or not calls:
                    raise ContextTransformError("invalid_tool_calls")
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or not isinstance(call.get("id"), str)
                        or not call["id"]
                        or call["id"] in declared
                    ):
                        raise ContextTransformError("ambiguous_tool_call")
                    declared.add(call["id"])
                    pending.add(call["id"])
    if pending:
        raise ContextTransformError("missing_tool_result")
    return results


class ContextModel:
    """Explicit synchronous model adapter; summary calls share the supplied run.

    The provider receives a fresh JSON payload. The summarizer is trusted host
    code representing one model invocation, with normalized exclusive usage.
    Policies see only safe hook metadata; content never enters HookContext.
    """

    def __init__(
        self,
        run,
        provider,
        *,
        config,
        adapter,
        summarizer,
        token_counter=None,
        dispatch=None,
        usage_reader=None,
        max_records=256,
    ):
        if (
            not isinstance(run, HarnessRun)
            or type(config) is not ContextPolicyConfig
            or type(adapter) is not ContextAdapter
            or type(summarizer) is not SummaryBackend
        ):
            raise ValueError(
                "typed run, context configuration, adapter and summarizer are required"
            )
        if not synchronous(provider) or (
            token_counter is not None and not synchronous(token_counter)
        ):
            raise ValueError("context adapter supports synchronous providers and counters")
        if type(max_records) is not int or not 1 <= max_records <= 10000:
            raise ValueError("max_records must be bounded")
        expected = context_policy(config)
        if run.harness.config.mode != "disabled" and not any(
            policy.config_hash == expected.config_hash
            and type(policy.evaluate) is _ContextAdmission
            for policy in run.harness.config.policies
        ):
            raise ValueError("register the matching context_policy in the harness before binding")
        self.run, self.provider = run, provider
        self.config, self.adapter, self.summarizer = config, adapter, summarizer
        self.token_counter, self.max_records = token_counter, max_records
        self._policy_hash = expected.config_hash
        self._records = {}
        self._lock = RLock()
        self._capture_failed = False
        self._summary = run.wrap(
            self._invoke_summary,
            boundary="model",
            branch_id=config.branch_id + ":summary",
            dispatch=summarizer.dispatch,
            usage_reader=lambda result: result.usage if type(result) is SummaryResult else None,
        )
        self._model = run.wrap(
            self._dispatch,
            boundary="model",
            branch_id=config.branch_id,
            dispatch=DispatchOptions() if dispatch is None else dispatch,
            usage_reader=usage_reader,
        )

    def export_evidence(self):
        with self._lock:
            return {"schema_version": CONTEXT_VERSION, "records": deepcopy(self._records)}

    def _count(self, payload):
        if self.token_counter is None:
            return ContextTokenCount()
        try:
            count = self.token_counter(deepcopy(payload))
        except Exception:
            raise ContextTransformError("token_counter_failed") from None
        if type(count) is not ContextTokenCount:
            raise ContextTransformError("invalid_token_count")
        return count

    def _fits(self, count):
        limit = self.config.max_input_tokens
        return limit is None or (
            count.provenance in EXACT_COUNTS and count.value is not None and count.value <= limit
        )

    def _invoke_summary(self, request, receipt):
        receipt["summary_invocations"] += 1
        result = self.summarizer.summarize(request)
        if type(result) is SummaryResult:
            usage = None if result.usage is None else result.usage.to_dict()
            if usage is not None and usage.get("usage_id") is not None:
                usage["usage_id"] = "sha256:" + fingerprint(usage["usage_id"])
            receipt["summary_usage"].append(usage)
        return result

    def _prepare(self, request, receipt):
        original = request.payload
        if not self.adapter.before_model or self.adapter.format != "text_tool_messages_v1":
            raise ContextTransformError("unsupported_adapter")
        if original.get("stream") is not None and original.get("stream") is not False:
            raise ContextTransformError("streaming_not_supported")
        results = _tool_results(original)
        optional, protected = (
            set(request.optional_tool_results),
            set(request.protected_tool_results),
        )
        if not (optional | protected) <= results.keys():
            raise ContextTransformError("unknown_tool_selection")
        before = self._count(original)
        receipt["original_tokens"] = before.to_dict()
        targets = [
            identity
            for identity, index in results.items()
            if identity in optional - protected
            and len(original["messages"][index]["content"]) > self.config.max_summary_chars
        ]
        if len(targets) > self.config.max_summaries:
            raise ContextTransformError("summary_count_limit")
        receipt["target_count"] = len(targets)
        receipt["protected_result_count"] = len(protected)
        if self.run.harness.config.mode == "shadow":
            receipt["status"] = "proposed" if targets else "unchanged"
            receipt["effective_tokens"] = before.to_dict()
            return original
        candidate = deepcopy(original)
        for identity in targets:
            index = results[identity]
            text = original["messages"][index]["content"]
            receipt["summary_attempts"] += 1
            try:
                summary = self._summary(
                    SummaryInput(text, self.config.max_summary_chars, fingerprint(text)), receipt
                )
            except HarnessControlError:
                raise
            except Exception:
                raise ContextTransformError("summary_failed") from None
            if (
                type(summary) is not SummaryResult
                or not summary.text.strip()
                or len(summary.text) > self.config.max_summary_chars
                or len(summary.text) >= len(text)
            ):
                raise ContextTransformError("invalid_summary")
            # Trust class, tool reference, call declaration and every other
            # message/option remain byte-for-byte values from the owned input.
            candidate["messages"][index]["content"] = summary.text
        _tool_results(candidate)
        after = self._count(candidate)
        receipt["proposed_tokens"] = after.to_dict()
        if (
            before.value is not None
            and after.value is not None
            and (before.provenance != after.provenance or before.reference != after.reference)
        ):
            raise ContextTransformError("token_counter_changed")
        if not self._fits(after):
            raise ContextTransformError("context_limit_unverified")
        if (
            targets
            and before.provenance in EXACT_COUNTS
            and after.provenance in EXACT_COUNTS
            and after.value >= before.value
        ):
            raise ContextTransformError("tokens_not_reduced")
        receipt.update(
            status="applied" if targets else "unchanged",
            transformed_hash=fingerprint(candidate),
            effective_tokens=after.to_dict(),
        )
        return candidate

    def _dispatch(self, request, receipt, start):
        original = request.payload
        try:
            prepared = self._prepare(request, receipt)
        except ContextTransformError as exc:
            receipt["error_code"] = exc.reason_code
            if self.run.harness.config.mode == "shadow":
                prepared = original
                receipt["status"] = "invalid_proposal"
            elif (
                self.config.on_invalid == "original"
                and not self.run.stopped
                and exc.reason_code != "streaming_not_supported"
                and self._fits(ContextTokenCount(**receipt["original_tokens"]))
            ):
                prepared = original
                receipt["status"] = "restored_original"
                receipt["effective_tokens"] = receipt["original_tokens"]
            else:
                raise
        receipt["effective_hash"] = fingerprint(prepared)
        receipt["preparation_ms"] = (perf_counter() - start) * 1000
        receipt["provider_invoked"] = True
        result = self.provider(deepcopy(prepared))
        if self.run.harness.config.mode == "enforce" and (
            isinstance(result, Iterator) or inspect.isawaitable(result)
        ):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise ContextTransformError("unexpected_streaming_result")
        return result

    def __call__(self, request):
        if type(request) is not ContextRequest:
            raise TypeError("context model requires ContextRequest")
        if self.run.harness.config.mode == "disabled":
            return self.provider(request.payload)
        if _ACTIVE.get() is not None:
            raise ContextTransformError("recursive_context_transform")
        identity = "ctx_" + uuid4().hex
        receipt = {
            "record_id": identity,
            "policy_id": self.config.policy_id,
            "policy_version": self.config.version,
            "policy_config_hash": self._policy_hash,
            "adapter": self.adapter.name,
            "mode": self.run.harness.config.mode,
            "quality_ref": self.config.quality_ref,
            "summarizer_ref": self.summarizer.reference,
            "harness_run_id": self.run.run_id,
            "harness_call_id": None,
            "original_hash": fingerprint(request.payload),
            "transformed_hash": None,
            "effective_hash": None,
            "status": "pending",
            "error_code": None,
            "summary_attempts": 0,
            "summary_invocations": 0,
            "summary_usage": [],
            "provider_invoked": False,
            "original_tokens": ContextTokenCount().to_dict(),
            "proposed_tokens": ContextTokenCount().to_dict(),
            "effective_tokens": ContextTokenCount().to_dict(),
            "preparation_ms": None,
            "provider_cache_outcome": "unobserved",
        }
        trace = current_trace()
        with self._lock, _EVIDENCE_LOCK:
            if self._capture_failed:
                raise ContextTransformError("invalid_trace_evidence")
            if len(self._records) >= self.max_records:
                raise ContextTransformError("evidence_limit")
            if trace is not None:
                envelope = trace.metadata.setdefault(
                    CONTEXT_KEY, {"schema_version": CONTEXT_VERSION, "records": {}}
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != CONTEXT_VERSION
                    or not isinstance(envelope.get("records"), dict)
                    or len(envelope["records"]) >= 10000
                ):
                    raise ContextTransformError("invalid_trace_evidence")
                envelope["records"][identity] = deepcopy(receipt)
            self._records[identity] = deepcopy(receipt)
        start = perf_counter()
        token = _ACTIVE.set(
            {"config": self.config, "run_id": self.run.run_id, "receipt": receipt, "used": False}
        )
        failure = None
        try:
            return self._model(request, receipt, start)
        except BaseException as exc:
            failure = exc
            receipt["status"] = (
                "cancelled"
                if not isinstance(exc, Exception)
                else "provider_failed"
                if receipt["provider_invoked"]
                else "blocked"
                if isinstance(exc, HarnessControlError)
                else "failed"
                if isinstance(exc, Exception)
                else "cancelled"
            )
            receipt["error_code"] = (
                "cancelled"
                if not isinstance(exc, Exception)
                else exc.reason_code
                if isinstance(exc, ContextTransformError)
                else "harness_control"
                if isinstance(exc, HarnessControlError)
                else "provider_error"
                if receipt["provider_invoked"]
                else "preparation_error"
                if isinstance(exc, Exception)
                else "cancelled"
            )
            raise
        finally:
            _ACTIVE.reset(token)
            if receipt["preparation_ms"] is None:
                receipt["preparation_ms"] = (perf_counter() - start) * 1000
            with self._lock, _EVIDENCE_LOCK:
                capture_error = False
                if trace is not None:
                    try:
                        current = trace.metadata[CONTEXT_KEY]
                        if (
                            current is not envelope
                            or not isinstance(current["records"], dict)
                            or current.get("schema_version") != CONTEXT_VERSION
                        ):
                            raise ValueError("capture changed")
                        current["records"][identity] = deepcopy(receipt)
                    except (KeyError, TypeError, ValueError):
                        capture_error = True
                        self._capture_failed = True
                        receipt["capture_error"] = "invalid_trace_evidence"
                self._records[identity] = deepcopy(receipt)
            if capture_error and failure is None:
                raise ContextTransformError("invalid_trace_evidence")
