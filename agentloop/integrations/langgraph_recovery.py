"""Narrow synchronous recovery through LangGraph's existing root checkpointer."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from agentloop.budget_types import DispatchOptions
from agentloop.checkpoint_state import (
    CheckpointError,
    _require,
    _text,
    restore_run,
    seal_run,
    validate_states,
)
from agentloop.context_types import fingerprint
from agentloop.events import new_run_id
from agentloop.harness import HarnessControlError, Hook
from agentloop.integrations.langgraph_harness import (
    SUPPORTED_VERSION,
    ControlledRunnable,
    _check_version,
)
from agentloop.loop_types import StepInfo
from agentloop.recovery_evidence import record_recovery
from agentloop.tracer import current_trace


@dataclass(frozen=True)
class RecoveryOutcome:
    run_id: str
    status: str
    value: object = field(repr=False, compare=False)


class LangGraphRecovery:
    """Host-owned records and atomic claims; only declared retry-safe root nodes.

    Bind one controller per owner/thread. Capture only after the original root
    invocation has unwound. The journal must serialize all actors for that thread.
    No asynchronous, streamed, subgraph or dynamic-interrupt recovery is claimed.
    """

    def __init__(
        self, controlled, store, *, owner, thread_id, graph_version, clock_domain, retry_safe_nodes
    ):
        _require(type(controlled) is ControlledRunnable, "unsupported_recovery_adapter")
        _check_version()
        self._adapter, self._app = controlled._adapter, controlled.app
        self._controlled = controlled
        _require(
            {Hook("iteration"), Hook("iteration", "after")} <= self._adapter.capabilities.hooks,
            "unsupported_recovery_adapter",
        )
        _require(
            callable(getattr(self._app, "get_state", None))
            and getattr(self._app, "checkpointer", None) is not None,
            "checkpoint_provider_required",
        )
        _require(
            getattr(store, "atomic_claim", None) is True
            and type(getattr(store, "durable", None)) is bool
            and getattr(store, "schema_version", None) == "1.0"
            and all(callable(getattr(store, name, None)) for name in ("save", "load", "claim")),
            "unsupported_recovery_store",
        )
        _require(
            all(_text(value) for value in (owner, thread_id, graph_version, clock_domain)),
            "invalid_owner",
        )
        _require(
            isinstance(retry_safe_nodes, (list, tuple, set, frozenset))
            and len(retry_safe_nodes) <= 256
            and all(_text(node) for node in retry_safe_nodes),
            "invalid_retry_safety",
        )
        validate_states(self._adapter._harness, {})
        self._store, self._owner, self._thread, self._clock = store, owner, thread_id, clock_domain
        self._safe = frozenset(retry_safe_nodes)
        self._recovery_config_hash = fingerprint(
            {"graph_version": graph_version, "retry_safe_nodes": sorted(self._safe)}
        )
        self._stream = fingerprint({"thread_id": thread_id, "namespace": ""})
        self._lock, self._running = RLock(), False
        self._claim, self._root_run_id = None, None
        self._last_run = None

    @property
    def last_run(self):
        return self._last_run

    def _snapshot(self):
        snapshot = self._app.get_state(
            {"configurable": {"thread_id": self._thread, "checkpoint_ns": ""}}
        )
        config = getattr(snapshot, "config", None)
        _require(
            type(config) is dict and type(config.get("configurable")) is dict, "missing_checkpoint"
        )
        selected = config["configurable"]
        _require(
            selected.get("thread_id") == self._thread and selected.get("checkpoint_ns", "") == "",
            "checkpoint_owner_mismatch",
        )
        _require(_text(selected.get("checkpoint_id")), "missing_checkpoint")
        pending, tasks = getattr(snapshot, "next", None), getattr(snapshot, "tasks", None)
        _require(
            isinstance(pending, (tuple, list))
            and len(pending) <= 256
            and all(_text(name) for name in pending),
            "invalid_checkpoint",
        )
        _require(isinstance(tasks, (tuple, list)) and len(tasks) <= 256, "invalid_checkpoint")
        declarations = []
        for task in tasks:
            _require(_text(getattr(task, "name", None)), "invalid_checkpoint")
            _require(getattr(task, "state", None) is None, "subgraph_recovery_unsupported")
            declarations.append(
                {
                    "name": task.name,
                    "failed": getattr(task, "error", None) is not None,
                    "interrupted": bool(getattr(task, "interrupts", ())),
                }
            )
        return {
            "checkpoint_id": selected["checkpoint_id"],
            "next": list(pending),
            "tasks": declarations,
        }

    def _enter(self):
        with self._lock:
            _require(not self._running, "recovery_already_running")
            self._running = True

    def capture(self):
        """Retire a quiescent run and save its ledger beside the host checkpoint.

        Failed persistence leaves the old run retired. Do not retry work; reconcile
        the journal and framework state with the host before another attempt.
        """
        self._enter()
        run = self._adapter.last_run
        reference = None
        try:
            _require(run is not None, "missing_run")
            if self._last_run is not None:
                _require(run is self._last_run, "run_lineage_changed")
                source_trace_id = self._last_trace_id
            else:
                source = self._controlled._last_invocation.get()
                _require(
                    isinstance(source, dict)
                    and source["finished"]
                    and source["run_id"] == run.run_id
                    and source["thread_id"] == self._thread
                    and source["checkpoint_ns"] == "",
                    "run_checkpoint_mismatch",
                )
                _require(source["durability"] == "sync", "synchronous_durability_required")
                source_trace_id = source["trace_id"]
            snapshot = self._snapshot()
            _require(bool(snapshot["next"]), "completed_checkpoint")
            state = seal_run(run, clock_domain=self._clock)
            self._controlled._recovery_bound = True
            record = {
                "schema_version": "1.0",
                "adapter_version": SUPPORTED_VERSION,
                "recovery_config_hash": self._recovery_config_hash,
                "owner_hash": fingerprint(self._owner),
                "stream_hash": self._stream,
                "root_run_id": self._root_run_id or run.run_id,
                "parent_trace_id": source_trace_id,
                "host": snapshot,
                "policy_state": state,
            }
            reference = self._store.save(
                self._owner,
                self._stream,
                record,
                previous_ref=None if self._claim is None else self._claim[0],
                request_id=None if self._claim is None else self._claim[1],
            )
            _require(_text(reference), "invalid_checkpoint_reference")
            record_recovery(
                "continue",
                "checkpoint_saved",
                reference=reference,
                parent_run_id=run.run_id,
                status="incomplete",
                store_durable=self._store.durable,
            )
            return reference
        except BaseException as exc:
            try:
                record_recovery(
                    "escalate",
                    exc.reason_code
                    if isinstance(exc, CheckpointError)
                    else "checkpoint_save_failed",
                    reference=reference,
                    parent_run_id=None if run is None else run.run_id,
                    store_durable=self._store.durable,
                )
            except BaseException:
                pass
            raise
        finally:
            with self._lock:
                self._running = False

    def resume(self, reference, *, request_id):
        self._enter()
        run = None
        parent = None
        try:
            _require(_text(reference) and _text(request_id), "invalid_checkpoint_reference")
            record = self._store.load(self._owner, self._stream, reference)
            _require(
                type(record) is dict
                and set(record)
                == {
                    "schema_version",
                    "adapter_version",
                    "recovery_config_hash",
                    "owner_hash",
                    "stream_hash",
                    "root_run_id",
                    "parent_trace_id",
                    "host",
                    "policy_state",
                },
                "invalid_checkpoint",
            )
            _require(
                record["schema_version"] == "1.0"
                and record["adapter_version"] == SUPPORTED_VERSION,
                "incompatible_schema",
            )
            _require(
                record["recovery_config_hash"] == self._recovery_config_hash,
                "incompatible_configuration",
            )
            _require(
                record["owner_hash"] == fingerprint(self._owner)
                and record["stream_hash"] == self._stream,
                "checkpoint_owner_mismatch",
            )
            _require(
                _text(record["root_run_id"]) and type(record["policy_state"]) is dict,
                "invalid_run_lineage",
            )
            _require(
                record["parent_trace_id"] is None or _text(record["parent_trace_id"]),
                "invalid_run_lineage",
            )
            parent = record["policy_state"].get("run_id")
            snapshot = self._snapshot()
            _require(snapshot == record["host"], "stale_checkpoint")
            _require(bool(snapshot["next"]), "completed_checkpoint")
            _require(
                not any(task["interrupted"] for task in snapshot["tasks"]),
                "dynamic_interrupt_requires_host",
            )
            _require(
                set(snapshot["next"]) <= self._safe
                and all(task["name"] in self._safe for task in snapshot["tasks"]),
                "side_effect_reconciliation_required",
            )
            trace = current_trace()
            _require(
                trace is None or trace.run_id != record["parent_trace_id"], "new_trace_required"
            )
            run = restore_run(
                self._adapter._harness,
                record["policy_state"],
                clock_domain=self._clock,
                run_id=new_run_id(),
            )
            _require(
                self._store.claim(
                    self._owner, self._stream, reference, request_id, fingerprint(record)
                )
                is True,
                "duplicate_or_stale_resume",
            )
            self._claim, self._root_run_id = (reference, request_id), record["root_run_id"]
            self._last_run = run
            self._last_trace_id = None if trace is None else trace.run_id
            self._controlled._recovery_bound = True
            record_recovery(
                "continue",
                "checkpoint_resume_admitted",
                reference=reference,
                parent_run_id=parent,
                attempt_run_id=run.run_id,
                store_durable=self._store.durable,
            )
            session = self._adapter._start(run)
            with self._adapter._scope(session, "main", root=True):
                # A resumed root is an explicit harness retry. Its children keep
                # their own model/tool accounting and any SDK retry identities.
                invoke = run.wrap(
                    self._app.invoke,
                    boundary="iteration",
                    branch_id="checkpoint.resume",
                    dispatch=DispatchOptions(
                        retry_source="harness", step=StepInfo("checkpoint.resume", retry_safe=True)
                    ),
                )
                value = invoke(
                    None,
                    {
                        "configurable": {
                            "thread_id": self._thread,
                            "checkpoint_ns": "",
                            "checkpoint_id": snapshot["checkpoint_id"],
                        }
                    },
                    durability="sync",
                )
                completed = not self._snapshot()["next"]
                if completed:
                    value = self._adapter._complete(session, value)
            status = "completed" if completed else "incomplete"
            record_recovery(
                "continue",
                "checkpoint_attempt_returned",
                reference=reference,
                parent_run_id=parent,
                attempt_run_id=run.run_id,
                status=status,
                store_durable=self._store.durable,
            )
            return RecoveryOutcome(run.run_id, status, value)
        except BaseException as exc:
            status = (
                "stopped"
                if isinstance(exc, HarnessControlError)
                else "cancelled"
                if not isinstance(exc, Exception)
                else "failed"
                if run is not None
                else "incomplete"
            )
            reason = (
                exc.reason_code if isinstance(exc, CheckpointError) else "recovery_execution_failed"
            )
            try:
                record_recovery(
                    "escalate",
                    reason,
                    reference=reference,
                    parent_run_id=parent,
                    attempt_run_id=None if run is None else run.run_id,
                    status=status,
                    store_durable=self._store.durable,
                )
            except BaseException:
                # Preserve host failures/cancellation even if its trace is broken.
                pass
            raise
        finally:
            with self._lock:
                self._running = False
