"""Payload-free checkpoint decisions joined to native harness provenance."""

from copy import deepcopy
from threading import RLock
from uuid import uuid4

from agentloop.checkpoint_state import _require
from agentloop.context_types import fingerprint
from agentloop.harness import Decision, Harness, HarnessConfig, HarnessControlError, Hook, Policy
from agentloop.tracer import current_trace

RECOVERY_KEY = "agentloop.recovery"
_LOCK = RLock()


def record_recovery(
    action,
    reason,
    *,
    reference=None,
    parent_run_id=None,
    attempt_run_id=None,
    status="incomplete",
    store_durable=False,
):
    """Record a separate admission audit, never a fresh execution budget."""
    identity = "recovery_" + uuid4().hex
    entry = {
        "recovery_id": identity,
        "action": action,
        "reason": reason,
        "checkpoint_ref_hash": None if reference is None else fingerprint(reference),
        "parent_run_id": parent_run_id,
        "attempt_run_id": attempt_run_id,
        "status": status,
        "store_durable": store_durable,
        "scope": "recovery_admission_only",
    }
    trace = current_trace()
    envelope = None
    if trace is not None:
        with _LOCK:
            envelope = trace.metadata.setdefault(
                RECOVERY_KEY, {"schema_version": "1.0", "records": {}}
            )
            _require(
                type(envelope) is dict
                and envelope.get("schema_version") == "1.0"
                and type(envelope.get("records")) is dict
                and len(envelope["records"]) < 10000,
                "invalid_recovery_evidence",
            )
            envelope["records"][identity] = deepcopy(entry)
    policy = Policy(
        "agentloop.recovery",
        "1.0",
        lambda context: Decision(action, reason, ("recovery:" + identity,)),
        hooks=frozenset({Hook("completion")}),
        actions=frozenset({"continue", "escalate"}),
    )
    audit = Harness(HarnessConfig("enforce", (policy,))).start_run("audit_" + uuid4().hex)
    try:
        audit.wrap(lambda: None, boundary="completion", branch_id="recovery")()
    except HarnessControlError:
        # This audit has no task work or task quota. Its escalation is re-raised
        # by the caller as a typed CheckpointError before host invocation.
        pass
    entry["audit_run_id"] = audit.run_id
    entry["audit_call_id"] = audit.results[0].call_id
    if trace is not None:
        with _LOCK:
            _require(trace.metadata.get(RECOVERY_KEY) is envelope, "invalid_recovery_evidence")
            envelope["records"][identity] = deepcopy(entry)
    return entry


def recovery_status(trace):
    """Return the latest declared attempt status for study adapters, if present."""
    envelope = trace.metadata.get(RECOVERY_KEY)
    if envelope is None:
        return None
    _require(
        type(envelope) is dict
        and envelope.get("schema_version") == "1.0"
        and type(envelope.get("records")) is dict
        and bool(envelope["records"]),
        "invalid_recovery_evidence",
    )
    latest = list(envelope["records"].values())[-1]
    _require(type(latest) is dict, "invalid_recovery_evidence")
    status = latest.get("status")
    _require(
        status in {"completed", "incomplete", "failed", "cancelled", "stopped"},
        "invalid_recovery_evidence",
    )
    return status
