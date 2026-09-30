"""Explicit operator-reviewed, bounded budget canaries with local rollback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Event, RLock
from uuid import uuid4

from agentloop.ablation_protocol import number, timestamp
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_types import identifier
from agentloop.harness import Decision, Harness, HarnessConfig, HarnessControlError, Hook, Policy
from agentloop.harness_evidence import HarnessDecisionRecord, append_records, empty_evidence
from agentloop.promotion_evidence import assess_promotion
from agentloop.promotion_types import BudgetProposal, OperatorApproval, utc_now
from agentloop.tracer import current_trace

PROMOTION_KEY = "agentloop.policy_promotion"
_TRACE_LOCK = RLock()


@dataclass(frozen=True)
class CanaryLease:
    lease_id: str
    configuration: HarnessConfig = field(repr=False)
    candidate: bool


@dataclass(frozen=True)
class CanaryOutcome:
    status: str
    quality_score: float | None
    latency_ms: float | None
    cost_usd: float | None
    cost_known: bool

    def __post_init__(self):
        if self.status not in {"completed", "failed", "cancelled", "stopped", "incomplete"}:
            raise ValueError("unsupported canary outcome")
        for name in ("quality_score", "latency_ms", "cost_usd"):
            number(
                getattr(self, name),
                name,
                nullable=True,
                maximum=1 if name == "quality_score" else None,
            )
        if type(self.cost_known) is not bool:
            raise ValueError("cost provenance must be explicit")


class BudgetPromotion:
    """One reviewed proposal; never learns or silently expands rollout scope.

    The trusted host authenticates operator attestations, applies each lease's
    configuration to real work, reports outcomes and calls tick during idle time.
    A kill switch/rollback changes future admission, never kills in-flight work.
    """

    def __init__(self, proposal, *, baseline_review_ref, clock=utc_now):
        if type(proposal) is not BudgetProposal:
            raise ValueError("BudgetProposal required")
        identifier(baseline_review_ref, "baseline_review_ref")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._proposal, self._data, self._clock = proposal, proposal.to_dict(), clock
        self._baseline = HarnessConfig("disabled")
        self._candidate = HarnessConfig(
            "enforce", (budget_policy(BudgetLimits(**self._data["candidate"])),)
        )
        self._versions = self._data["protocol"]["specification"]["versions"]
        self._workload = self._data["protocol"]["specification"]["workload_id"]
        self._state, self._approval, self._started = "candidate", None, None
        self._last_time = None
        self._leases, self._outcomes, self._records = {}, {}, []
        self._native = empty_evidence()
        self._lock = RLock()
        self._kill = Event()
        self._baseline_review_ref = baseline_review_ref
        self._record("candidate_registered", "continue")

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def kill_switch(self):
        return self._kill

    def export_evidence(self):
        with self._lock:
            return {
                "schema_version": "1.0",
                "proposal_hash": self._proposal.proposal_hash,
                "state": self._state,
                "admitted": len(self._leases),
                "reported": len(self._outcomes),
                "missing": len(self._leases) - len(self._outcomes),
                "outcomes": deepcopy(self._outcomes),
                "records": deepcopy(self._records),
                "native_evidence": deepcopy(self._native),
            }

    def shadow_configuration(self):
        with self._lock:
            if self._state != "candidate":
                raise ValueError("shadow transition requires a new candidate")
            self._state = "shadow"
            self._record("candidate_shadowed", "continue")
            return HarnessConfig("shadow", self._candidate.policies)

    def review(self, approval):
        if type(approval) is not OperatorApproval:
            raise ValueError("explicit OperatorApproval required")
        with self._lock:
            if self._state != "shadow":
                raise ValueError("operator review follows explicit shadow configuration")
            now = self._now()
            assessment = assess_promotion(self._proposal, now=now.isoformat())
            approval_reasons = []
            if not approval.approved:
                approval_reasons.append("operator_rejected")
            if approval.proposal_hash != self._proposal.proposal_hash:
                approval_reasons.append("approval_proposal_mismatch")
            if not timestamp(approval.approved_at) <= now < timestamp(approval.valid_until):
                approval_reasons.append("approval_expired_or_future")
            if assessment["eligible"]:
                latest_evidence = max(
                    timestamp(self._data["capability"]["tested_at"]),
                    timestamp(self._data["calibration"]["validity"]["as_of"]),
                    *(timestamp(row["started_at"]) for row in self._data["observations"]),
                )
                if timestamp(approval.approved_at) < latest_evidence:
                    approval_reasons.append("approval_predates_evidence")
            valid = not approval_reasons
            assessment.update(operator_approved=valid, approval_reasons=approval_reasons)
            if not valid or not assessment["eligible"]:
                self._state = "rejected"
                self._record(
                    "operator_rejected" if not approval.approved else "promotion_ineligible",
                    "escalate",
                    reasons=assessment["reasons"] + approval_reasons,
                )
                assessment["state"] = self._state
                return assessment
            self._approval, self._state = approval, "approved"
            age = timedelta(days=self._data["thresholds"]["max_age_days"])
            self._eligibility_until = min(
                timestamp(approval.valid_until),
                timestamp(self._data["valid_until"]),
                timestamp(self._data["calibration"]["validity"]["valid_until"]),
                timestamp(self._data["protocol"]["specification"]["frozen_at"]) + age,
                timestamp(self._data["capability"]["tested_at"]) + age,
                timestamp(self._data["calibration"]["validity"]["as_of"]) + age,
            )
            self._record("canary_approved", "continue", review_ref=approval.review_ref)
            assessment["state"] = self._state
            return assessment

    def begin_canary(self):
        with self._lock:
            if self._state != "approved":
                raise ValueError("explicit eligible approval required before canary")
            self._started = self._now()
            self._state = "canary"
            self._check(self._started)
            if self._state == "canary":
                self._record("canary_started", "continue")

    def admit(self, *, scope, versions, workload_id, opted_in=False):
        identifier(scope, "scope")
        if type(opted_in) is not bool:
            raise ValueError("rollout opt-in must be explicit")
        with self._lock:
            self._check(self._now())
            if self._state != "canary" or not opted_in or scope not in self._approval.scopes:
                return CanaryLease("baseline_" + uuid4().hex, self._baseline, False)
            if versions != self._versions or workload_id != self._workload:
                self._rollback("runtime_versions_changed")
                return CanaryLease("baseline_" + uuid4().hex, self._baseline, False)
            if len(self._leases) >= self._data["canary"]["max_admissions"]:
                return CanaryLease("baseline_" + uuid4().hex, self._baseline, False)
            lease = CanaryLease("canary_" + uuid4().hex, self._candidate, True)
            self._leases[lease.lease_id] = lease
            self._record("canary_admitted", "continue", lease_id=lease.lease_id, scope=scope)
            return lease

    def report(self, lease, outcome):
        if type(lease) is not CanaryLease or type(outcome) is not CanaryOutcome:
            raise ValueError("typed lease and outcome required")
        with self._lock:
            if self._leases.get(lease.lease_id) is not lease or lease.lease_id in self._outcomes:
                raise ValueError("canary lease is unknown or already reported")
            self._check(self._now())
            self._outcomes[lease.lease_id] = {
                "status": outcome.status,
                "quality_score": outcome.quality_score,
                "latency_ms": outcome.latency_ms,
                "cost_usd": outcome.cost_usd,
                "cost_known": outcome.cost_known,
            }
            limits = self._data["canary"]
            passed = (
                outcome.status == "completed"
                and outcome.quality_score is not None
                and outcome.quality_score >= limits["min_quality_score"]
                and outcome.latency_ms is not None
                and outcome.latency_ms <= limits["max_latency_ms"]
                and outcome.cost_known
                and outcome.cost_usd is not None
                and outcome.cost_usd <= limits["max_cost_usd"]
            )
            self._record(
                "canary_outcome_passed" if passed else "canary_outcome_failed",
                "continue" if passed else "escalate",
                lease_id=lease.lease_id,
            )
            if not passed:
                self._rollback("canary_regression_or_missing_measurement")
            elif self._state == "canary" and len(self._outcomes) == limits["max_admissions"]:
                self._state = "canary_passed"
                self._record("canary_finished_requires_review", "continue")

    def tick(self):
        with self._lock:
            self._check(self._now())
            return self._state

    def rollback(self):
        with self._lock:
            self._rollback("operator_rollback")

    def _now(self):
        try:
            now = timestamp(self._clock())
        except BaseException:
            self._rollback("clock_unavailable")
            raise
        if self._last_time is not None and now < self._last_time:
            self._rollback("clock_moved_backwards")
            raise ValueError("promotion clock moved backwards")
        self._last_time = now
        return now

    def _check(self, now):
        if self._state not in {"approved", "canary"}:
            return
        if self._kill.is_set():
            self._rollback("kill_switch")
        elif now >= self._eligibility_until:
            self._rollback("approval_or_evidence_expired")
        elif (
            self._started is not None
            and (now - self._started).total_seconds() >= self._data["canary"]["max_duration_s"]
        ):
            self._rollback("canary_deadline_or_missing_outcomes")

    def _rollback(self, reason):
        if self._state == "rolled_back":
            return
        self._state = "rolled_back"
        self._record(
            reason,
            "escalate",
            rollback_config_hash=self._baseline.config_hash,
            baseline_review_ref=self._baseline_review_ref,
        )

    def _record(self, reason, action, **details):
        entry = {
            "sequence": len(self._records),
            "reason": reason,
            "action": action,
            "state": self._state,
            "proposal_hash": self._proposal.proposal_hash,
            "policy_config_hash": self._data["policy_config_hash"],
            "details": details,
        }
        if len(self._records) >= 3008:
            self._state = "rolled_back"
            raise ValueError("promotion evidence limit")
        policy = Policy(
            "agentloop.promotion",
            "1.0",
            lambda context: Decision(
                action, reason, ("proposal:sha256:" + self._proposal.proposal_hash,)
            ),
            hooks=frozenset({Hook("completion")}),
            actions=frozenset({"continue", "escalate"}),
        )
        audit = Harness(HarnessConfig("enforce", (policy,))).start_run(
            "promotion_audit_" + uuid4().hex
        )
        try:
            audit.wrap(lambda: None, boundary="completion", branch_id="promotion")()
        except HarnessControlError:
            pass
        except BaseException:
            self._state = "rolled_back"
            raise
        native = audit.export_evidence()
        append_records(
            self._native,
            tuple(HarnessDecisionRecord.from_dict(item) for item in native["decisions"].values()),
            native["policies"],
        )
        entry.update(audit_run_id=audit.run_id, audit_call_id=audit.results[0].call_id)
        self._records.append(entry)
        trace = current_trace()
        if trace is not None:
            with _TRACE_LOCK:
                envelope = trace.metadata.setdefault(
                    PROMOTION_KEY, {"schema_version": "1.0", "records": []}
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != "1.0"
                    or not isinstance(envelope.get("records"), list)
                    or len(envelope["records"]) >= 10000
                ):
                    self._state = "rolled_back"
                    raise ValueError("invalid promotion trace evidence")
                envelope["records"].append(deepcopy(entry))
