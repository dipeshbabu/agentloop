"""The Postgres trace store.

Split out of ``agentloop.store`` (issue #407); ``agentloop.store`` re-exports
every name, so existing imports keep working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentloop.cursor_validation import decode_cursor, encode_cursor
from agentloop.findings import build_diagnosis
from agentloop.intervention_store import InterventionStoreMixin
from agentloop.migrations import apply_postgres_migrations
from agentloop.store_common import (
    _aggregate_cost_status,
    _audit_row,
    _build_optimization_queue,
    _delete_runs,
    _find_api_key_row,
    _finding_row,
    _normalized_estimated_cost,
    _normalized_model_call_count,
    _record_audit,
    _report_cost_completeness,
    _retention_cutoff,
    _sources_allowing,
    _timestamp_text,
    _validate_new_status,
    _validate_page_limit,
    _with_labels,
    validate_label_filter,
)
from agentloop.store_keys import (
    _API_KEY_PREFIX_LENGTH,
    _require_key_prefix,
    hash_api_key,
    new_api_key,
)
from agentloop.store_types import (
    _LEGACY_LIST_SAFETY_LIMIT,
    _QUEUE_MAX_FINDINGS,
    DEFAULT_PAGE_SIZE,
    FindingNotFoundError,
    FindingTransitionError,
    TraceProjectConflictError,
)
from agentloop.tracer import AgentTrace


@dataclass
class PostgresTraceStore(InterventionStoreMixin):
    _postgres_interventions = True
    dsn: str | None = None
    password_file: str | None = None
    _ready: bool = field(default=False, init=False, repr=False, compare=False)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Install postgres support with: uv sync --locked --extra postgres"
            ) from exc
        if self.dsn:
            return psycopg.connect(self.dsn)
        connect_kwargs: dict[str, Any] = {}
        if self.password_file:
            connect_kwargs["password"] = _read_postgres_password(self.password_file)
        return psycopg.connect("", **connect_kwargs)

    def init(self) -> None:
        with self._connect() as conn:
            apply_postgres_migrations(conn)
        self._ready = True

    def _ensure_init(self) -> None:
        # Checking migrations costs a connection and several queries, so do it
        # once per store rather than on every call.
        if not self._ready:
            self.init()

    def create_api_key(self, project_id: str, name: str, actor: str = "local") -> dict[str, Any]:
        self._ensure_init()
        api_key = new_api_key()
        prefix = api_key[:_API_KEY_PREFIX_LENGTH]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,)
            )
            conn.execute(
                "INSERT INTO api_keys(key_hash, project_id, name, prefix) VALUES (%s, %s, %s, %s)",
                (hash_api_key(api_key), project_id, name, prefix),
            )
            _record_audit(conn, "%s", "api_key.created", project_id, actor, prefix, {"name": name})
        return {"api_key": api_key, "project_id": project_id, "name": name, "prefix": prefix}

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None:
        self._ensure_init()
        prefix = api_key[:_API_KEY_PREFIX_LENGTH]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_hash, project_id, name, prefix FROM api_keys WHERE prefix = %s",
                (prefix,),
            ).fetchall()
            row = _find_api_key_row(api_key, rows, lambda candidate: candidate[0])
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s",
                (row[0],),
            )
            return {"project_id": row[1], "name": row[2], "prefix": row[3]}

    def list_api_keys(self, project_id: str) -> list[dict[str, Any]]:
        """Key metadata for one project; key values and hashes are never returned."""
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT prefix, name, created_at, last_used_at FROM api_keys "
                "WHERE project_id = %s ORDER BY created_at, prefix",
                (project_id,),
            ).fetchall()
        return [
            {
                "project_id": project_id,
                "prefix": row[0],
                "name": row[1],
                "created_at": _timestamp_text(row[2]),
                "last_used_at": _timestamp_text(row[3]),
            }
            for row in rows
        ]

    def list_audit_events(
        self, project_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Newest audit events first, optionally for one project."""
        limit = _validate_page_limit(limit)
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, occurred_at, project_id, action, actor, target, details_json "
                "FROM audit_events WHERE (%s::text IS NULL OR project_id = %s) "
                "ORDER BY id DESC LIMIT %s",
                (project_id, project_id, limit),
            ).fetchall()
        return [_audit_row(tuple(row)) for row in rows]

    def revoke_api_key(self, project_id: str, prefix: str, actor: str = "local") -> int:
        """Delete a project's keys with this prefix; returns how many were revoked."""
        _require_key_prefix(prefix)
        self._ensure_init()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM api_keys WHERE project_id = %s AND prefix = %s",
                (project_id, prefix),
            )
            revoked = int(cursor.rowcount or 0)
            if revoked:
                _record_audit(
                    conn, "%s", "api_key.revoked", project_id, actor, prefix, {"revoked": revoked}
                )
            return revoked

    def delete_trace(
        self, run_id: str, project_id: str, actor: str = "local"
    ) -> dict[str, int] | None:
        """Delete one trace of a project and its findings, usage and interventions."""
        self._ensure_init()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM traces WHERE run_id = %s AND project_id = %s", (run_id, project_id)
            ).fetchone()
            if exists is None:
                return None
            deleted = _delete_runs(conn, "%s", project_id, [run_id])
            _record_audit(conn, "%s", "trace.deleted", project_id, actor, run_id, deleted)
            return deleted

    def purge_traces(
        self, older_than_days: float, project_id: str | None = None, actor: str = "local"
    ) -> dict[str, int]:
        """Delete traces stored more than ``older_than_days`` ago, optionally in one project."""
        cutoff = _retention_cutoff(older_than_days)
        self._ensure_init()
        totals = {"traces": 0, "findings": 0, "usage_events": 0, "interventions": 0}
        with self._connect() as conn:
            query = "SELECT project_id, run_id FROM traces WHERE created_at < %s"
            params: tuple[Any, ...] = (cutoff,)
            if project_id:
                query += " AND project_id = %s"
                params += (project_id,)
            by_project: dict[str, list[str]] = {}
            for row in conn.execute(query, params).fetchall():
                by_project.setdefault(row[0], []).append(row[1])
            for project, run_ids in by_project.items():
                deleted = _delete_runs(conn, "%s", project, run_ids)
                for name, count in deleted.items():
                    totals[name] += count
                _record_audit(
                    conn,
                    "%s",
                    "traces.purged",
                    project,
                    actor,
                    None,
                    {**deleted, "older_than_days": float(older_than_days)},
                )
        return totals

    def _upsert_trace(
        self, conn, trace: AgentTrace, project_id: str, report: dict[str, Any]
    ) -> None:
        cost_status, priced_calls, unavailable_calls = _report_cost_completeness(report)
        model_call_count, _ = _normalized_model_call_count(report)
        estimated_cost_usd = _normalized_estimated_cost(report)
        conn.execute(
            "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,)
        )
        cursor = conn.execute(
            """
            INSERT INTO traces(run_id, project_id, name, started_at, payload_json, total_runtime_ms,
                estimated_cost_usd, cost_status, priced_model_call_count,
                unavailable_model_call_count, model_call_count, tool_call_count, retry_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(run_id) DO UPDATE SET project_id=excluded.project_id, name=excluded.name, started_at=excluded.started_at,
            payload_json=excluded.payload_json, total_runtime_ms=excluded.total_runtime_ms, estimated_cost_usd=excluded.estimated_cost_usd,
            cost_status=excluded.cost_status, priced_model_call_count=excluded.priced_model_call_count,
            unavailable_model_call_count=excluded.unavailable_model_call_count,
            model_call_count=excluded.model_call_count, tool_call_count=excluded.tool_call_count, retry_count=excluded.retry_count
            WHERE traces.project_id = excluded.project_id
            """,
            (
                trace.run_id,
                project_id,
                trace.name,
                trace.started_at,
                json.dumps(trace.to_dict()),
                report.get("total_runtime_ms", 0),
                estimated_cost_usd,
                cost_status,
                priced_calls,
                unavailable_calls,
                model_call_count,
                report.get("tool_call_count", 0),
                report.get("retry_count", 0),
            ),
        )
        if cursor.rowcount == 0:
            raise TraceProjectConflictError(
                f"run_id {trace.run_id!r} already belongs to another project"
            )

    def _upsert_usage(self, conn, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        cost_status, priced_calls, unavailable_calls = _report_cost_completeness(report)
        model_call_count, _ = _normalized_model_call_count(report)
        estimated_cost_usd = _normalized_estimated_cost(report)
        conn.execute(
            """
            INSERT INTO usage_events(project_id, run_id, total_runtime_ms, estimated_cost_usd,
                input_tokens, output_tokens, cost_status, priced_model_call_count,
                unavailable_model_call_count, model_call_count, tool_call_count, retry_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(project_id, run_id) DO UPDATE SET
                total_runtime_ms=excluded.total_runtime_ms,
                estimated_cost_usd=excluded.estimated_cost_usd,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cost_status=excluded.cost_status,
                priced_model_call_count=excluded.priced_model_call_count,
                unavailable_model_call_count=excluded.unavailable_model_call_count,
                model_call_count=excluded.model_call_count,
                tool_call_count=excluded.tool_call_count,
                retry_count=excluded.retry_count,
                created_at=now()
            """,
            (
                project_id,
                run_id,
                report.get("total_runtime_ms", 0),
                estimated_cost_usd,
                report.get("input_tokens", 0),
                report.get("output_tokens", 0),
                cost_status,
                priced_calls,
                unavailable_calls,
                model_call_count,
                report.get("tool_call_count", 0),
                report.get("retry_count", 0),
            ),
        )

    def _upsert_findings(self, conn, diagnosis: dict[str, Any], project_id: str) -> None:
        conn.execute(
            "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,)
        )
        run_id = diagnosis["run_id"]
        new_finding_ids: list[str] = []
        for finding in diagnosis.get("findings", []):
            savings = finding.get("savings", {})
            rewrite = finding.get("rewrite", {})
            new_finding_ids.append(finding["finding_id"])
            conn.execute(
                """
                INSERT INTO trace_findings(project_id, run_id, finding_id, type, severity, title, confidence, patchable, estimated_latency_savings_ms, estimated_cost_savings_usd, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(project_id, run_id, finding_id) DO UPDATE SET
                type=excluded.type, severity=excluded.severity, title=excluded.title, confidence=excluded.confidence,
                patchable=excluded.patchable, estimated_latency_savings_ms=excluded.estimated_latency_savings_ms,
                estimated_cost_savings_usd=excluded.estimated_cost_savings_usd, payload_json=excluded.payload_json,
                status=CASE WHEN trace_findings.status = 'superseded' THEN 'detected' ELSE trace_findings.status END,
                updated_at=now()
                """,
                (
                    project_id,
                    run_id,
                    finding["finding_id"],
                    finding["type"],
                    finding["severity"],
                    finding["title"],
                    finding["confidence"],
                    bool(rewrite.get("patchable")),
                    savings.get("estimated_latency_savings_ms", 0),
                    savings.get("estimated_cost_savings_usd", 0),
                    json.dumps(finding),
                ),
            )
        if not diagnosis.get("analysis_complete", True):
            return
        conn.execute(
            """
            UPDATE trace_findings
            SET status = 'superseded', updated_at = now()
            WHERE project_id = %s AND run_id = %s
              AND status NOT IN ('resolved', 'dismissed', 'superseded')
              AND finding_id != ALL(%s)
            """,
            (project_id, run_id, new_finding_ids),
        )

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self._ensure_init()
        # Analyse before opening the transaction so the write lock is held only
        # for the inserts.
        report = trace.report()
        diagnosis = build_diagnosis(trace, report=report)
        with self._connect() as conn:
            self._upsert_trace(conn, trace, project_id, report)
            self._upsert_usage(conn, project_id, trace.run_id, report)
            self._upsert_findings(conn, diagnosis, project_id)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_init()
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, cost_status, priced_model_call_count, unavailable_model_call_count, created_at::text FROM traces WHERE project_id = %s ORDER BY created_at DESC, run_id DESC LIMIT %s",
                    (project_id, _LEGACY_LIST_SAFETY_LIMIT),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, cost_status, priced_model_call_count, unavailable_model_call_count, created_at::text FROM traces ORDER BY created_at DESC, run_id DESC LIMIT %s",
                    (_LEGACY_LIST_SAFETY_LIMIT,),
                ).fetchall()
        keys = [
            "run_id",
            "project_id",
            "name",
            "started_at",
            "total_runtime_ms",
            "estimated_cost_usd",
            "cost_status",
            "priced_model_call_count",
            "unavailable_model_call_count",
            "created_at",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def list_traces_page(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        label: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        after_created_at, after_run_id = (
            decode_cursor(cursor, expected_parts=2) if cursor else (None, None)
        )
        limit = _validate_page_limit(limit)
        keys = [
            "run_id",
            "project_id",
            "name",
            "started_at",
            "total_runtime_ms",
            "estimated_cost_usd",
            "cost_status",
            "priced_model_call_count",
            "unavailable_model_call_count",
            "created_at",
            "labels",
        ]
        label_key, label_value = validate_label_filter(label) if label else (None, None)
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd,
                    cost_status, priced_model_call_count, unavailable_model_call_count,
                    created_at::text, payload_json -> 'metadata' -> 'labels'
                FROM traces
                WHERE project_id = COALESCE(%s, project_id)
                  AND (%s = 0 OR (created_at, run_id) < (%s, %s))
                  AND (%s::text IS NULL OR
                       payload_json -> 'metadata' -> 'labels' ->> %s::text = %s::text)
                ORDER BY created_at DESC, run_id DESC
                LIMIT %s
                """,
                (
                    project_id,
                    1 if cursor else 0,
                    after_created_at,
                    after_run_id,
                    label_key,
                    label_key,
                    label_value,
                    limit + 1,
                ),
            ).fetchall()
        items = [_with_labels(dict(zip(keys, row, strict=True))) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor([last["created_at"], last["run_id"]])
        return {"items": items, "next_cursor": next_cursor}

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None:
        self._ensure_init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    "SELECT payload_json FROM traces WHERE run_id = %s AND project_id = %s",
                    (run_id, project_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload_json FROM traces WHERE run_id = %s", (run_id,)
                ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return AgentTrace.from_dict(payload)

    def save_diagnosis(self, diagnosis: dict[str, Any], project_id: str = "default") -> None:
        self._ensure_init()
        with self._connect() as conn:
            self._upsert_findings(conn, diagnosis, project_id)

    def list_findings(
        self,
        project_id: str | None = None,
        status: str | None = None,
        *,
        type: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at::text, updated_at::text
                FROM trace_findings
                WHERE project_id = COALESCE(%s, project_id)
                  AND status = COALESCE(%s, status)
                  AND type = COALESCE(%s, type)
                  AND title = COALESCE(%s, title)
                ORDER BY
                    CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    COALESCE(estimated_cost_savings_usd, -1) DESC,
                    COALESCE(estimated_latency_savings_ms, -1) DESC,
                    updated_at DESC
                LIMIT %s
                """,
                (
                    project_id or None,
                    status or None,
                    type or None,
                    title or None,
                    _LEGACY_LIST_SAFETY_LIMIT,
                ),
            ).fetchall()
        keys = [
            "project_id",
            "run_id",
            "finding_id",
            "type",
            "severity",
            "title",
            "confidence",
            "patchable",
            "status",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
            "payload_json",
            "created_at",
            "updated_at",
        ]
        return [_finding_row(dict(zip(keys, row, strict=True))) for row in rows]

    def finding_counts(self, project_id: str | None = None) -> dict[str, int]:
        """How many findings are stored and how many are patchable, without loading them."""
        self._ensure_init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN patchable THEN 1 ELSE 0 END), 0) "
                "FROM trace_findings WHERE project_id = COALESCE(%s, project_id)",
                (project_id or None,),
            ).fetchone()
        return {"total": int(row[0]), "patchable": int(row[1])}

    def list_findings_page(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        after_updated_at, after_run_id, after_finding_id = (
            decode_cursor(cursor, expected_parts=3) if cursor else (None, None, None)
        )
        limit = _validate_page_limit(limit)
        keys = [
            "project_id",
            "run_id",
            "finding_id",
            "type",
            "severity",
            "title",
            "confidence",
            "patchable",
            "status",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
            "payload_json",
            "created_at",
            "updated_at",
        ]
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at::text, updated_at::text
                FROM trace_findings
                WHERE project_id = COALESCE(%s, project_id)
                  AND status = COALESCE(%s, status)
                  AND (%s = 0 OR (updated_at, run_id, finding_id) < (%s, %s, %s))
                ORDER BY updated_at DESC, run_id DESC, finding_id DESC
                LIMIT %s
                """,
                (
                    project_id,
                    status,
                    1 if cursor else 0,
                    after_updated_at,
                    after_run_id,
                    after_finding_id,
                    limit + 1,
                ),
            ).fetchall()
        items = [_finding_row(dict(zip(keys, row, strict=True))) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor([last["updated_at"], last["run_id"], last["finding_id"]])
        return {"items": items, "next_cursor": next_cursor}

    def update_finding_status(
        self, project_id: str, run_id: str, finding_id: str, new_status: str
    ) -> dict[str, Any]:
        _validate_new_status(new_status)
        allowed_sources = _sources_allowing(new_status)
        keys = [
            "project_id",
            "run_id",
            "finding_id",
            "type",
            "severity",
            "title",
            "confidence",
            "patchable",
            "status",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
            "payload_json",
            "created_at",
            "updated_at",
        ]
        self._ensure_init()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE trace_findings
                SET status = %s, updated_at = now()
                WHERE project_id = %s AND run_id = %s AND finding_id = %s
                  AND status = ANY(%s)
                """,
                (new_status, project_id, run_id, finding_id, allowed_sources),
            )
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT status FROM trace_findings WHERE project_id = %s AND run_id = %s AND finding_id = %s",
                    (project_id, run_id, finding_id),
                ).fetchone()
                if existing is None:
                    raise FindingNotFoundError(
                        f"finding {finding_id!r} not found for run {run_id!r} in project {project_id!r}"
                    )
                raise FindingTransitionError(existing[0], new_status)
            row = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at::text, updated_at::text
                FROM trace_findings WHERE project_id = %s AND run_id = %s AND finding_id = %s
                """,
                (project_id, run_id, finding_id),
            ).fetchone()
        return _finding_row(dict(zip(keys, row, strict=True)))

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at::text, updated_at::text
                FROM trace_findings
                WHERE project_id = COALESCE(%s, project_id)
                  AND status NOT IN ('resolved', 'dismissed', 'superseded')
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (project_id or None, _QUEUE_MAX_FINDINGS),
            ).fetchall()
        keys = [
            "project_id",
            "run_id",
            "finding_id",
            "type",
            "severity",
            "title",
            "confidence",
            "patchable",
            "status",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
            "payload_json",
            "created_at",
            "updated_at",
        ]
        findings = [_finding_row(dict(zip(keys, row, strict=True))) for row in rows]
        return _build_optimization_queue(findings, project_id)

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._upsert_usage(conn, project_id, run_id, report)

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        self._ensure_init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(priced_model_call_count),0), COALESCE(SUM(unavailable_model_call_count),0), COALESCE(SUM(CASE WHEN cost_status IN ('partial', 'unknown') THEN 1 ELSE 0 END),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(priced_model_call_count),0), COALESCE(SUM(unavailable_model_call_count),0), COALESCE(SUM(CASE WHEN cost_status IN ('partial', 'unknown') THEN 1 ELSE 0 END),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events"
                ).fetchone()
        keys = [
            "run_count",
            "total_runtime_ms",
            "estimated_cost_usd",
            "priced_model_call_count",
            "unavailable_model_call_count",
            "incomplete_cost_run_count",
            "input_tokens",
            "output_tokens",
            "model_call_count",
            "tool_call_count",
            "retry_count",
        ]
        result = dict(zip(keys, row, strict=True))
        incomplete_run_count = int(result.pop("incomplete_cost_run_count"))
        result["cost_status"] = _aggregate_cost_status(
            int(result["priced_model_call_count"]),
            int(result["unavailable_model_call_count"]),
            incomplete_run_count=incomplete_run_count,
        )
        if result["cost_status"] not in {"complete", "empty"}:
            result["estimated_cost_usd"] = None
        if project_id:
            result["project_id"] = project_id
        return result


def _read_postgres_password(path: str) -> str:
    """Read a file-backed Postgres password without including it in errors or logs."""
    try:
        password = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Unable to read Postgres password file: {path}") from exc
    if not password:
        raise RuntimeError(f"Postgres password file is empty: {path}")
    return password
