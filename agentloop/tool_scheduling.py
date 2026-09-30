"""Bounded host-declared tool scheduling with cooperative stopping and no retries."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass
from threading import Event, RLock
from time import monotonic, perf_counter
from types import MappingProxyType
from uuid import uuid4

from agentloop.events import utc_now_iso
from agentloop.harness import Decision, HarnessControlError, HarnessRun, Hook, Policy
from agentloop.scheduling_plan import plan_tools
from agentloop.scheduling_types import (
    SCHEDULE_KEY,
    SCHEDULE_VERSION,
    ScheduleConfig,
    ScheduledResult,
    SchedulingError,
    ToolContext,
    ToolOutcome,
)
from agentloop.tracer import current_trace

_ACTIVE = ContextVar("agentloop_scheduled_tool", default=None)
_EVIDENCE_LOCK = RLock()


@dataclass(frozen=True)
class _ScheduleAdmission:
    config: ScheduleConfig

    def __call__(self, context):
        if not context.branch_id.startswith(self.config.branch_id + ":"):
            return Decision("continue", "schedule_not_selected")
        active = _ACTIVE.get()
        if (
            active is None
            or active["used"]
            or active["run_id"] != context.run_id
            or active["branch"] != context.branch_id
            or active["config"] != self.config
        ):
            return Decision("deny", "schedule_adapter_required")
        active["used"] = True
        active["record"]["harness_call_id"] = context.call_id
        if active["unknown"] and self.config.on_unknown == "error":
            return Decision("deny", "unknown_scheduling_safety")
        return Decision("continue", "declared_schedule_admitted")


def scheduling_policy(config):
    if type(config) is not ScheduleConfig:
        raise ValueError("scheduling policy requires ScheduleConfig")
    return Policy(
        config.policy_id,
        config.version,
        _ScheduleAdmission(config),
        hooks=frozenset({Hook("tool")}),
        actions=frozenset({"continue", "deny"}),
        priority=50,
        configuration=config.to_dict(),
    )


class ToolScheduler:
    """Execute once, drain in-flight work, and return results in submission order.

    Deadlines/cancellation stop new admission; Python cannot forcibly terminate a
    running thread. Already-started callbacks must return or cooperate. The method
    drains them before returning or re-raising an interruption. Never retries.
    """

    def __init__(self, run, *, config, max_records=64, max_claimed_calls=4096):
        if not isinstance(run, HarnessRun) or type(config) is not ScheduleConfig:
            raise ValueError("typed run and scheduling configuration required")
        expected = scheduling_policy(config)
        if run.harness.config.mode != "disabled" and not any(
            policy.config_hash == expected.config_hash
            and type(policy.evaluate) is _ScheduleAdmission
            for policy in run.harness.config.policies
        ):
            raise ValueError("register matching scheduling_policy before binding")
        if (
            "sync" not in run.harness.capabilities.execution_kinds
            or not {Hook("tool"), Hook("tool", "after")} <= run.harness.capabilities.hooks
        ):
            raise SchedulingError("unsupported_lifecycle")
        if (
            type(max_records) is not int
            or not 1 <= max_records <= 1000
            or type(max_claimed_calls) is not int
            or not 1 <= max_claimed_calls <= 100000
        ):
            raise ValueError("scheduler evidence and call registries must be bounded")
        self._run, self._config = run, config
        self._max_records, self._max_claimed = max_records, max_claimed_calls
        self._lock, self._records, self._claimed = RLock(), {}, set()
        self._running = False
        self._capture_failed = False
        self._last_result = None

    @property
    def run(self):
        return self._run

    @property
    def config(self):
        return self._config

    @property
    def last_result(self):
        """Retain partial/completed outputs even when caller interruption propagates."""
        with self._lock:
            return self._last_result

    def export_evidence(self):
        with self._lock:
            return {"schema_version": SCHEDULE_VERSION, "records": deepcopy(self._records)}

    def execute(self, calls, *, timeout_s=None, cancel_event=None):
        if timeout_s is not None and (
            type(timeout_s) not in {int, float} or not 0 < timeout_s < float("inf")
        ):
            raise ValueError("timeout_s must be finite and positive")
        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise ValueError("cancel_event must be threading.Event")
        mode = self.run.harness.config.mode
        plan = plan_tools(calls, self.config, enforce=mode == "enforce")
        by_id = {call.call_id: call for call in calls}
        identity = "schedule_" + uuid4().hex
        trace = current_trace() if mode != "disabled" else None
        records = {
            call.call_id: {
                "status": "pending",
                "harness_call_id": None,
                "callback_invoked": False,
                "callback_completed": False,
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "reason": None,
            }
            for call in calls
        }
        concurrency = plan["proposed_concurrency"] if mode == "enforce" else 1
        receipt = {
            "schedule_id": identity,
            "schema_version": SCHEDULE_VERSION,
            "policy_config_hash": scheduling_policy(self.config).config_hash,
            "mode": mode,
            "plan": plan,
            "actual_concurrency_limit": concurrency,
            "peak_running": 0,
            "dispatch_order": [],
            "completion_order": [],
            "calls": records,
            "status": "running",
            "stop_reason": None,
            "timeout_semantics": "stop new admission and drain already-started callbacks; no thread termination",
        }
        with self._lock, _EVIDENCE_LOCK:
            if self._running or self._claimed.intersection(by_id):
                raise SchedulingError("already_started")
            if self._capture_failed:
                raise SchedulingError("invalid_trace_evidence")
            if (
                len(self._records) >= self._max_records
                or len(self._claimed) + len(calls) > self._max_claimed
            ):
                raise SchedulingError("evidence_limit")
            if trace is not None:
                envelope = trace.metadata.setdefault(
                    SCHEDULE_KEY, {"schema_version": SCHEDULE_VERSION, "records": {}}
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != SCHEDULE_VERSION
                    or not isinstance(envelope.get("records"), dict)
                    or len(envelope["records"]) >= 10000
                ):
                    raise SchedulingError("invalid_trace_evidence")
                envelope["records"][identity] = deepcopy(receipt)
            self._running = True
            self._claimed.update(by_id)
            if mode != "disabled":
                self._records[identity] = deepcopy(receipt)
        stop = Event()
        deadline = None if timeout_s is None else monotonic() + timeout_s
        outcomes, finished, active, pending = {}, {}, {}, set(by_id)
        running = 0
        failure = None
        executor = None

        def call_one(call, prerequisites):
            nonlocal running
            record = records[call.call_id]
            returned = []
            context = ToolContext(
                call.call_id, MappingProxyType(prerequisites), stop, cancel_event, deadline
            )
            if context.cancellation_requested:
                record.update(status="not_started", reason="admission_stopped")
                return ToolOutcome(call.call_id, "not_started")
            branch = self.config.branch_id + ":" + call.call_id

            def invoke():
                nonlocal running
                if context.cancellation_requested:
                    # No side effect begins after the cooperative stop is observed.
                    return _NOT_STARTED
                with self._lock:
                    running += 1
                    receipt["peak_running"] = max(receipt["peak_running"], running)
                    receipt["dispatch_order"].append(call.call_id)
                    record.update(callback_invoked=True, status="running", started_at=utc_now_iso())
                started = perf_counter()
                try:
                    value = call.invoke(context)
                    returned.append(value)
                    record["callback_completed"] = True
                    return value
                finally:
                    with self._lock:
                        running -= 1
                        record["ended_at"] = utc_now_iso()
                        record["duration_ms"] = (perf_counter() - started) * 1000

            protected = self.run.wrap(
                invoke,
                boundary="tool",
                branch_id=branch,
                dispatch=call.dispatch,
                usage_reader=(
                    lambda value: None if value is _NOT_STARTED else call.usage_reader(value)
                )
                if call.usage_reader is not None
                else None,
                error_usage_reader=call.error_usage_reader,
            )
            binding = {
                "used": False,
                "config": self.config,
                "branch": branch,
                "run_id": self.run.run_id,
                "record": record,
                "unknown": call.call_id in plan["unknown_safety"],
            }
            token = _ACTIVE.set(binding)
            try:
                value = protected()
                status = "not_started" if value is _NOT_STARTED else "completed"
                record["status"] = status
                return ToolOutcome(call.call_id, status, None if value is _NOT_STARTED else value)
            except BaseException as exc:
                status = (
                    "cancelled"
                    if not isinstance(exc, Exception)
                    else "blocked"
                    if isinstance(exc, HarnessControlError)
                    else "failed"
                )
                record.update(
                    status=status,
                    reason="harness_control"
                    if isinstance(exc, HarnessControlError)
                    else "cancelled"
                    if status == "cancelled"
                    else "tool_error",
                )
                if returned:
                    record["status"] = "completed"
                    return ToolOutcome(call.call_id, "completed", returned[0], exc)
                return ToolOutcome(call.call_id, status, error=exc)
            finally:
                _ACTIVE.reset(token)

        def worker(call, prerequisites):
            try:
                result = call_one(call, prerequisites)
            except BaseException as exc:
                records[call.call_id].update(status="failed", reason="dispatch_setup_failed")
                result = ToolOutcome(call.call_id, "failed", error=exc)
            with self._lock:
                finished[call.call_id] = result
            return result

        def accept(result):
            nonlocal failure
            if result.call_id in outcomes:
                return
            outcomes[result.call_id] = result
            receipt["completion_order"].append(result.call_id)
            if result.status != "completed" and self.config.on_error == "stop":
                stop.set()
                receipt["stop_reason"] = receipt["stop_reason"] or "tool_failure"
            if isinstance(result.error, HarnessControlError):
                stop.set()
                receipt["stop_reason"] = receipt["stop_reason"] or "harness_control"
            if result.error is not None and not isinstance(result.error, Exception):
                failure = failure or result.error
                stop.set()
                receipt["stop_reason"] = "cancelled"

        try:
            if concurrency > 1:
                executor = ThreadPoolExecutor(max_workers=concurrency)
            while pending or active:
                if cancel_event is not None and cancel_event.is_set():
                    stop.set()
                    receipt["stop_reason"] = "cancelled"
                if deadline is not None and monotonic() >= deadline:
                    stop.set()
                    receipt["stop_reason"] = receipt["stop_reason"] or "deadline"
                if self.run.stopped:
                    stop.set()
                    receipt["stop_reason"] = receipt["stop_reason"] or "harness_stopped"
                for call_id in plan["serial_order"]:
                    if call_id not in pending or stop.is_set() or len(active) >= concurrency:
                        continue
                    dependencies = plan["effective_dependencies"][call_id]
                    if not all(dependency in outcomes for dependency in dependencies):
                        continue
                    pending.remove(call_id)
                    if any(
                        outcomes[dependency].status != "completed" for dependency in dependencies
                    ):
                        records[call_id].update(
                            status="dependency_failed", reason="prerequisite_failed"
                        )
                        accept(ToolOutcome(call_id, "dependency_failed"))
                        continue
                    call = by_id[call_id]
                    selected = call.depends_on if call.depends_on is not None else tuple(outcomes)
                    prerequisites = {key: outcomes[key].value for key in selected}
                    if executor is None:
                        accept(worker(call, prerequisites))
                        break
                    future = executor.submit(copy_context().run, worker, call, prerequisites)
                    active[future] = call_id
                if stop.is_set():
                    for call_id in pending:
                        records[call_id].update(status="not_started", reason=receipt["stop_reason"])
                        outcomes[call_id] = ToolOutcome(call_id, "not_started")
                    pending.clear()
                if active:
                    completed, _ = wait(active, timeout=0.02, return_when=FIRST_COMPLETED)
                    for future in completed:
                        active.pop(future)
                        accept(future.result())
            receipt["status"] = (
                "completed"
                if all(result.status == "completed" for result in outcomes.values())
                and receipt["stop_reason"] is None
                else "partial"
            )
        except BaseException as exc:
            failure = exc
            stop.set()
            receipt.update(
                status="interrupted",
                stop_reason="cancelled" if not isinstance(exc, Exception) else "scheduler_error",
            )
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
            for future, call_id in active.items():
                if future.done():
                    accept(future.result())
            for result in finished.values():
                accept(result)
            for call_id in by_id.keys() - outcomes.keys():
                records[call_id].update(
                    status="not_started", reason=receipt["stop_reason"] or "interrupted"
                )
                outcomes[call_id] = ToolOutcome(call_id, "not_started")
            with self._lock, _EVIDENCE_LOCK:
                self._running = False
                self._last_result = ScheduledResult(
                    identity,
                    tuple(outcomes[call.call_id] for call in calls),
                    receipt["stop_reason"],
                )
                capture_error = False
                if trace is not None:
                    try:
                        current = trace.metadata[SCHEDULE_KEY]
                        if (
                            current is not envelope
                            or current.get("schema_version") != SCHEDULE_VERSION
                            or not isinstance(current["records"], dict)
                        ):
                            raise ValueError()
                        current["records"][identity] = deepcopy(receipt)
                    except (KeyError, TypeError, ValueError):
                        capture_error = self._capture_failed = True
                        receipt["capture_error"] = "invalid_trace_evidence"
                if mode != "disabled":
                    self._records[identity] = deepcopy(receipt)
            if capture_error and failure is None:
                raise SchedulingError("invalid_trace_evidence")
        if failure is not None:
            raise failure
        return self._last_result


_NOT_STARTED = object()
