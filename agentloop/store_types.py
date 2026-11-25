"""Shared constants, errors and the TraceStore protocol for the trace stores.

Split out of ``agentloop.store`` (issue #407); ``agentloop.store`` re-exports
every name, so existing imports keep working.
"""

from __future__ import annotations

from typing import Any, Protocol

from agentloop.interventions import InterventionRecord
from agentloop.tracer import AgentTrace

# Finding lifecycle. `detected` is the initial state for every newly diagnosed
# finding. `superseded` is not reachable through update_finding_status(); it is
# assigned automatically when a finding an operator hasn't triaged (detected /
# accepted) disappears from a later diagnosis of the same run. Terminal human
# decisions (resolved, dismissed) are never overwritten by re-diagnosis. Every
# state can be `reopened` back to `detected`.
FINDING_STATUSES = frozenset({"detected", "accepted", "resolved", "dismissed", "superseded"})
_INACTIVE_FINDING_STATUSES = frozenset({"resolved", "dismissed", "superseded"})
ALLOWED_FINDING_TRANSITIONS: dict[str, frozenset[str]] = {
    "detected": frozenset({"accepted", "dismissed"}),
    "accepted": frozenset({"resolved", "dismissed"}),
    "resolved": frozenset({"detected"}),
    "dismissed": frozenset({"detected"}),
    "superseded": frozenset({"detected"}),
}

# Compatibility default for the unpaginated list_traces()/list_findings() calls:
# generous enough that no real deployment notices it, but bounded so a single
# call can't load an unbounded table into memory. Callers that need more than
# this should use list_traces_page()/list_findings_page().
_LEGACY_LIST_SAFETY_LIMIT = 10_000

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# optimization_queue() clusters unresolved findings in Python; bound the window
# to the most recently updated findings per project instead of loading a
# project's entire finding history.
_QUEUE_MAX_FINDINGS = 5000

_COST_STATUSES = frozenset({"complete", "partial", "unknown", "empty"})
_MAX_DATABASE_INTEGER = 2_147_483_647


class TraceProjectConflictError(ValueError):
    """Raised when a run ID is already owned by a different project."""


class FindingNotFoundError(LookupError):
    """Raised when a finding does not exist within the given project."""


class FindingTransitionError(ValueError):
    """Raised when a status transition is not allowed from the finding's current status."""

    def __init__(self, current_status: str, new_status: str):
        self.current_status = current_status
        self.new_status = new_status
        super().__init__(f"cannot transition finding from {current_status!r} to {new_status!r}")


class TraceStore(Protocol):
    def init(self) -> None: ...

    def _ensure_init(self) -> None:
        """Run init() unless this store has already done so."""
        ...

    def get_finding_snapshot(
        self, run_id: str, finding_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None: ...

    def save_intervention(
        self, record: InterventionRecord | dict[str, Any], project_id: str = "default"
    ) -> dict[str, Any]: ...

    def get_intervention(
        self, intervention_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None: ...

    def create_api_key(
        self, project_id: str, name: str, actor: str = "local"
    ) -> dict[str, Any]: ...

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None: ...

    def list_api_keys(self, project_id: str) -> list[dict[str, Any]]: ...

    def revoke_api_key(self, project_id: str, prefix: str, actor: str = "local") -> int: ...

    def list_audit_events(
        self, project_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None: ...

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def list_traces_page(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        label: tuple[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None: ...

    def delete_trace(
        self, run_id: str, project_id: str, actor: str = "local"
    ) -> dict[str, int] | None: ...

    def purge_traces(
        self, older_than_days: float, project_id: str | None = None, actor: str = "local"
    ) -> dict[str, int]: ...

    def save_diagnosis(self, diagnosis: dict[str, Any], project_id: str = "default") -> None: ...

    def list_findings(
        self,
        project_id: str | None = None,
        status: str | None = None,
        *,
        type: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_findings_page(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def finding_counts(self, project_id: str | None = None) -> dict[str, int]: ...

    def update_finding_status(
        self, project_id: str, run_id: str, finding_id: str, new_status: str
    ) -> dict[str, Any]: ...

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None: ...

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]: ...
