"""Shared SQLite/Postgres intervention contract; dialect differences stay local."""

from __future__ import annotations

import json
from typing import Any

from agentloop.interventions import (
    InterventionConflictError,
    InterventionRecord,
    InterventionReferenceError,
    canonical_json,
    trace_fingerprint,
)
from agentloop.tracer import AgentTrace


class InterventionStoreMixin:
    _postgres_interventions = False

    def _intervention_sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._postgres_interventions else sql

    def get_finding_snapshot(
        self, run_id: str, finding_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                self._intervention_sql(
                    "SELECT payload_json FROM trace_findings WHERE project_id = ? AND run_id = ? AND finding_id = ?"
                ),
                (project_id, run_id, finding_id),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]

    def get_intervention(
        self, intervention_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                self._intervention_sql(
                    "SELECT payload_json FROM interventions WHERE project_id = ? AND intervention_id = ?"
                ),
                (project_id, intervention_id),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def save_intervention(
        self, record: InterventionRecord | dict[str, Any], project_id: str = "default"
    ) -> dict[str, Any]:
        validated = InterventionRecord.from_dict(
            record.to_dict() if isinstance(record, InterventionRecord) else record
        )
        payload = validated.to_dict()
        serialized = canonical_json(payload)
        self.init()
        with self._connect() as conn:
            if not self._postgres_interventions:
                conn.execute("BEGIN IMMEDIATE")
            lookup = self._intervention_sql(
                "SELECT payload_json FROM interventions WHERE project_id = ? AND intervention_id = ?"
            )
            identity = (project_id, validated.intervention_id)
            existing = conn.execute(lookup, identity).fetchone()
            if existing is not None:
                if canonical_json(json.loads(existing[0])) != serialized:
                    raise InterventionConflictError(
                        "intervention identity already has different evidence"
                    )
                return json.loads(existing[0])
            for condition in ("baseline", "candidate"):
                query = "SELECT payload_json FROM traces WHERE project_id = ? AND run_id = ?"
                if self._postgres_interventions:
                    query += " FOR SHARE"
                row = conn.execute(
                    self._intervention_sql(query), (project_id, payload[f"{condition}_run_id"])
                ).fetchone()
                if row is None:
                    raise InterventionReferenceError("baseline or candidate trace not found")
                native = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if (
                    trace_fingerprint(AgentTrace.from_dict(native))
                    != payload["trace_fingerprints"][condition]
                ):
                    raise InterventionConflictError(
                        "stored trace differs from the intervention's source trace"
                    )
            for finding_id in payload["target_finding_ids"]:
                query = "SELECT finding_id FROM trace_findings WHERE project_id = ? AND run_id = ? AND finding_id = ?"
                if self._postgres_interventions:
                    query += " FOR SHARE"
                if (
                    conn.execute(
                        self._intervention_sql(query),
                        (project_id, payload["baseline_run_id"], finding_id),
                    ).fetchone()
                    is None
                ):
                    raise InterventionReferenceError("target baseline finding not found")
            conn.execute(
                self._intervention_sql(
                    "INSERT INTO interventions(project_id, intervention_id, baseline_run_id, candidate_run_id, payload_json) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, intervention_id) DO NOTHING"
                ),
                (*identity, payload["baseline_run_id"], payload["candidate_run_id"], serialized),
            )
            # Concurrent Postgres writers may reach INSERT together. Only an
            # identical winner satisfies the retry contract.
            stored = conn.execute(lookup, identity).fetchone()
            if canonical_json(json.loads(stored[0])) != serialized:
                raise InterventionConflictError(
                    "intervention identity already has different evidence"
                )
            return json.loads(stored[0])
