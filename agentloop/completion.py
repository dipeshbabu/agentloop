"""Single-use, bounded before-finish verification on a shared harness run."""

from __future__ import annotations

from asyncio import CancelledError
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from agentloop.completion_types import (
    COMPLETION_KEY,
    COMPLETION_VERSION,
    CheckResult,
    CompletionCandidate,
    CompletionConfig,
    CompletionResult,
    RepairBackend,
    VerificationContext,
    VerificationFeedback,
)
from agentloop.context_types import fingerprint
from agentloop.harness import (
    Decision,
    HarnessControlError,
    HarnessDeniedError,
    HarnessRun,
    Hook,
    Policy,
)
from agentloop.tracer import current_trace

_ACTIVE = ContextVar("agentloop_completion", default=None)
_EVIDENCE_LOCK = RLock()


@dataclass(frozen=True)
class _CompletionAdmission:
    config: CompletionConfig

    def __call__(self, context):
        if context.branch_id != self.config.branch_id:
            return Decision("continue", "completion_not_selected")
        active = _ACTIVE.get()
        if (
            active is None
            or active["used"]
            or active["run"] != context.run_id
            or active["config"] is not self.config
        ):
            return Decision("deny", "completion_adapter_required")
        active["used"] = True
        active["record"]["harness_call_id"] = context.call_id
        active["record"]["decision_id"] = context.decision_id
        return Decision(active["action"], active["reason"], (active["reference"],))


def completion_policy(config):
    if type(config) is not CompletionConfig:
        raise ValueError("completion policy requires CompletionConfig")
    return Policy(
        config.policy_id,
        config.version,
        _CompletionAdmission(config),
        hooks=frozenset({Hook("completion")}),
        actions=frozenset({"continue", "deny", "stop", "escalate"}),
        priority=50,
        configuration=config.to_dict(),
    )


class CompletionGate:
    """Verify once before the host releases a final marker; never retract a stream.

    Deadlines are cooperative. Trusted callbacks must use the provided remaining
    time for their I/O or subprocess timeout. Late results cannot pass.
    """

    def __init__(self, run, *, config, repair=None):
        if not isinstance(run, HarnessRun) or type(config) is not CompletionConfig:
            raise ValueError("typed run and completion configuration required")
        if run.harness.config.mode != "disabled" and not any(
            type(policy.evaluate) is _CompletionAdmission and policy.evaluate.config is config
            for policy in run.harness.config.policies
        ):
            raise ValueError("register matching completion_policy before binding")
        needed = {
            Hook("completion"),
            Hook("completion", "after"),
            Hook("tool"),
            Hook("tool", "after"),
        }
        if repair is not None:
            if type(repair) is not RepairBackend:
                raise ValueError("repair must be RepairBackend")
            needed.update({Hook(repair.boundary), Hook(repair.boundary, "after")})
        if (
            "sync" not in run.harness.capabilities.execution_kinds
            or not needed <= run.harness.capabilities.hooks
        ):
            raise ValueError("unsupported completion lifecycle")
        if bool(config.max_repairs) != (repair is not None):
            raise ValueError("bounded repair configuration and backend must be supplied together")
        self._run, self._config, self._repair = run, config, repair
        self._lock, self._used, self._records = RLock(), False, {}

    @property
    def run(self):
        return self._run

    @property
    def config(self):
        return self._config

    def export_evidence(self):
        with self._lock:
            return {"schema_version": COMPLETION_VERSION, "records": deepcopy(self._records)}

    def complete(self, candidate, *, cancel_event=None):
        if type(candidate) is not CompletionCandidate:
            raise ValueError("candidate must be CompletionCandidate")
        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise ValueError("cancel_event must be threading.Event")
        with self._lock:
            if self._used:
                raise ValueError("completion gate is single-use")
            self._used = True
        mode = self.run.harness.config.mode
        if mode == "disabled":
            return CompletionResult("unverified", candidate, False, 0)
        identity = "completion_" + uuid4().hex
        trace = current_trace()
        receipt = {
            "completion_id": identity,
            "mode": mode,
            "policy_config_hash": completion_policy(self.config).config_hash,
            "attempts": [],
            "repairs": [],
            "status": "running",
            "independent_task_evaluation": False,
            "already_streamed": candidate.already_streamed,
        }
        envelope = None
        if trace is not None:
            with _EVIDENCE_LOCK:
                envelope = trace.metadata.setdefault(
                    COMPLETION_KEY, {"schema_version": COMPLETION_VERSION, "records": {}}
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != COMPLETION_VERSION
                    or not isinstance(envelope.get("records"), dict)
                    or len(envelope["records"]) >= 10000
                ):
                    raise ValueError("invalid completion trace evidence")
                envelope["records"][identity] = deepcopy(receipt)
        deadline = monotonic() + self.config.total_timeout_s
        verification_remaining = self.config.verification_timeout_s
        repairs = 0
        failure = None
        try:
            while True:
                self._cancel(cancel_event)
                attempt = {
                    "index": len(receipt["attempts"]),
                    "candidate_hash": fingerprint(candidate.value),
                    "present": candidate.present,
                    "already_streamed": candidate.already_streamed,
                    "checks": [],
                    "harness_call_id": None,
                    "decision_id": None,
                }
                receipt["attempts"].append(attempt)
                feedback = []
                if not candidate.present:
                    reason = "completion_missing"
                elif candidate.already_streamed:
                    reason = "completion_already_streamed"
                elif monotonic() >= deadline or verification_remaining <= 0:
                    reason = "completion_timeout"
                else:
                    started = monotonic()
                    context = VerificationContext(
                        min(deadline, started + verification_remaining), cancel_event
                    )
                    for check_index, check in enumerate(self.config.checks):
                        self._cancel(cancel_event)
                        branch = f"{identity}:check:{attempt['index']}:{check_index}"
                        item = {**check.declaration(), "status": "pending", "branch_id": branch}
                        attempt["checks"].append(item)
                        if context.remaining_s <= 0:
                            item["status"] = "timeout"
                            feedback.append(VerificationFeedback(check.check_id, "timeout"))
                            continue

                        def invoke_check(candidate, context, check=check):
                            self._cancel(cancel_event)
                            if context.remaining_s <= 0:
                                raise TimeoutError("verification admission deadline expired")
                            return check.invoke(candidate, context)

                        try:
                            result = self.run.wrap(
                                invoke_check,
                                boundary="tool",
                                branch_id=branch,
                                dispatch=check.dispatch,
                                usage_reader=check.usage_reader,
                                error_usage_reader=check.error_usage_reader,
                            )(candidate, context)
                        except HarnessControlError:
                            item["status"] = "blocked"
                            raise
                        except Exception:
                            item["status"] = "error"
                            result = None
                        except BaseException:
                            item["status"] = "cancelled"
                            raise
                        if cancel_event is not None and cancel_event.is_set():
                            item["status"] = "cancelled"
                            self._cancel(cancel_event)
                        detail = ""
                        if context.remaining_s <= 0:
                            item["status"] = "timeout"
                        elif item["status"] == "pending":
                            if type(result) is not CheckResult:
                                item["status"] = "invalid"
                            else:
                                item["status"] = "passed" if result.passed else "failed"
                                detail = result.feedback
                        item["feedback_hash"] = fingerprint(detail)
                        if item["status"] != "passed":
                            feedback.append(
                                VerificationFeedback(check.check_id, item["status"], detail)
                            )
                    verification_remaining -= monotonic() - started
                    reason = "completion_verified" if not feedback else "completion_checks_failed"
                passed = reason == "completion_verified"
                if not passed and not feedback:
                    feedback.append(VerificationFeedback("completion", reason))
                can_repair = (
                    not passed
                    and reason != "completion_already_streamed"
                    and repairs < self.config.max_repairs
                    and monotonic() < deadline
                    and verification_remaining > 0
                )
                action = "continue" if passed else "deny" if can_repair else self.config.on_failure
                attempt.update(action=action, reason=reason, repair_requested=can_repair)
                binding = {
                    "used": False,
                    "run": self.run.run_id,
                    "config": self.config,
                    "record": attempt,
                    "action": action,
                    "reason": reason,
                    "reference": f"completion:{identity}:attempt:{attempt['index']}",
                }
                token = _ACTIVE.set(binding)
                try:
                    self.run.wrap(
                        lambda: None, boundary="completion", branch_id=self.config.branch_id
                    )()
                except HarnessDeniedError as exc:
                    if not can_repair or any(
                        proposal.decision.action != "continue"
                        and proposal.policy_id != self.config.policy_id
                        for proposal in exc.result.proposals
                    ):
                        raise
                finally:
                    _ACTIVE.reset(token)
                if mode == "shadow":
                    receipt["status"] = "shadow"
                    return CompletionResult("unverified", candidate, False, 0)
                if passed:
                    self._cancel(cancel_event)
                    if monotonic() >= deadline:
                        raise TimeoutError("completion deadline expired before release")
                    receipt["status"] = "accepted"
                    return CompletionResult("accepted", candidate, True, repairs)
                self._cancel(cancel_event)
                if monotonic() >= deadline:
                    raise TimeoutError("completion deadline expired before repair")
                branch = f"{identity}:repair:{repairs + 1}"
                repair_record = {"index": repairs + 1, "status": "pending", "branch_id": branch}
                receipt["repairs"].append(repair_record)
                backend = self._repair
                repairs += 1

                def invoke_repair(candidate, feedback, context):
                    self._cancel(cancel_event)
                    if context.remaining_s <= 0:
                        raise TimeoutError("repair admission deadline expired")
                    return backend.invoke(candidate, feedback, context)

                try:
                    candidate = self.run.wrap(
                        invoke_repair,
                        boundary=backend.boundary,
                        branch_id=branch,
                        dispatch=replace(backend.dispatch, retry_source="harness"),
                        usage_reader=backend.usage_reader,
                        error_usage_reader=backend.error_usage_reader,
                    )(candidate, tuple(feedback), VerificationContext(deadline, cancel_event))
                    if type(candidate) is not CompletionCandidate:
                        raise ValueError("repair must return CompletionCandidate")
                    repair_record["status"] = "returned"
                except BaseException:
                    repair_record["status"] = "failed"
                    raise
        except BaseException as exc:
            failure = exc
            receipt["status"] = (
                "blocked"
                if isinstance(exc, HarnessControlError)
                else "cancelled"
                if not isinstance(exc, Exception)
                else "error"
            )
            raise
        finally:
            with self._lock, _EVIDENCE_LOCK:
                self._records[identity] = deepcopy(receipt)
                if trace is not None:
                    if (
                        trace.metadata.get(COMPLETION_KEY) is not envelope
                        or envelope.get("schema_version") != COMPLETION_VERSION
                        or not isinstance(envelope.get("records"), dict)
                    ):
                        receipt["capture_error"] = "invalid_trace_evidence"
                        receipt["status"] = "evidence_error"
                        self._records[identity] = deepcopy(receipt)
                        if failure is None:
                            raise ValueError("invalid completion trace evidence")
                    else:
                        envelope["records"][identity] = deepcopy(receipt)

    @staticmethod
    def _cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("completion cancelled")
