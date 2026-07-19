from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentloop.config import (
    get_postgres_dsn,
    get_postgres_password_file,
    postgres_connection_source,
)
from agentloop.findings import build_diagnosis
from agentloop.migrations import apply_postgres_migrations, apply_sqlite_migrations
from agentloop.savings import SavingsItem, select_compatible
from agentloop.tracer import AgentTrace

_API_KEY_PREFIX_LENGTH = 10
_API_KEY_SALT_BYTES = 16
_API_KEY_HASH_BYTES = 32
_API_KEY_SCRYPT_N = 2**14
_API_KEY_SCRYPT_R = 8
_API_KEY_SCRYPT_P = 1

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


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor cannot be decoded."""


class TraceStore(Protocol):
    def init(self) -> None: ...

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]: ...

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None: ...

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None: ...

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def list_traces_page(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None: ...

    def save_diagnosis(self, diagnosis: dict[str, Any], project_id: str = "default") -> None: ...

    def list_findings(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]: ...

    def list_findings_page(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def update_finding_status(
        self, project_id: str, run_id: str, finding_id: str, new_status: str
    ) -> dict[str, Any]: ...

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None: ...

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]: ...


def hash_api_key(api_key: str) -> str:
    salt = secrets.token_bytes(_API_KEY_SALT_BYTES)
    digest = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=_API_KEY_SCRYPT_N,
        r=_API_KEY_SCRYPT_R,
        p=_API_KEY_SCRYPT_P,
        dklen=_API_KEY_HASH_BYTES,
    )
    return "scrypt$v1${salt}${digest}".format(
        salt=_encode_hash_bytes(salt),
        digest=_encode_hash_bytes(digest),
    )


def verify_api_key_hash(api_key: str, encoded_hash: str) -> bool:
    if not api_key.startswith("al_") or len(api_key) > 512:
        return False
    parts = encoded_hash.split("$")
    if len(parts) != 4 or parts[:2] != ["scrypt", "v1"]:
        return False
    try:
        salt = _decode_hash_bytes(parts[2])
        expected = _decode_hash_bytes(parts[3])
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return False
    if len(salt) != _API_KEY_SALT_BYTES or len(expected) != _API_KEY_HASH_BYTES:
        return False
    actual = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=_API_KEY_SCRYPT_N,
        r=_API_KEY_SCRYPT_R,
        p=_API_KEY_SCRYPT_P,
        dklen=_API_KEY_HASH_BYTES,
    )
    return hmac.compare_digest(actual, expected)


def _encode_hash_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_hash_bytes(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


def new_api_key() -> str:
    return "al_" + secrets.token_urlsafe(32)


def encode_cursor(parts: list[Any]) -> str:
    raw = json.dumps(parts, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> list[Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parts = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}") from exc
    if not isinstance(parts, list):
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    return parts


def _validate_page_limit(limit: int) -> int:
    if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"page limit must be between 1 and {MAX_PAGE_SIZE}, got {limit!r}")
    return limit


def _validate_new_status(new_status: str) -> str:
    if new_status not in FINDING_STATUSES:
        raise ValueError(
            f"unknown finding status {new_status!r}; expected one of {sorted(FINDING_STATUSES)}"
        )
    return new_status


def _sources_allowing(new_status: str) -> list[str]:
    return [
        source for source, targets in ALLOWED_FINDING_TRANSITIONS.items() if new_status in targets
    ]


# The widest source set any target status has (detected <- resolved/dismissed/superseded).
# update_finding_status() pads its source list to this fixed width so the SQL text is
# static regardless of new_status, rather than building a variable-length `IN (...)`.
_MAX_TRANSITION_SOURCES = max(len(_sources_allowing(status)) for status in FINDING_STATUSES)


def _padded_sources(new_status: str) -> list[str]:
    sources = _sources_allowing(new_status)
    return sources + [""] * (_MAX_TRANSITION_SOURCES - len(sources))


def _finding_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_payload = row.pop("payload_json")
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    row["patchable"] = bool(row["patchable"])
    row["estimated_latency_savings_ms"] = float(row["estimated_latency_savings_ms"] or 0)
    row["estimated_cost_savings_usd"] = (
        None
        if row["estimated_cost_savings_usd"] is None
        else float(row["estimated_cost_savings_usd"])
    )
    row["finding"] = payload
    return row


def _report_cost_completeness(report: dict[str, Any]) -> tuple[str, int, int]:
    """Return validated status and priced/unavailable call counts for storage."""

    breakdown = report.get("cost_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    total, count_is_valid = _normalized_model_call_count(report)
    if not count_is_valid:
        return "unknown", 0, 0
    if _normalized_estimated_cost(report) is None and total:
        return "unknown", 0, total
    status = report.get("cost_status")
    status = status if status in _COST_STATUSES else None

    try:
        priced = max(0, int(breakdown.get("priced_model_call_count", 0) or 0))
        unavailable = max(0, int(breakdown.get("unavailable_model_call_count", 0) or 0))
    except (OverflowError, TypeError, ValueError):
        priced = unavailable = 0

    if priced + unavailable != total:
        if status == "unknown":
            priced, unavailable = 0, total
        elif status == "partial" and total > 1:
            priced, unavailable = total - 1, 1
        else:
            priced, unavailable = total, 0
    return _aggregate_cost_status(priced, unavailable), priced, unavailable


def _normalized_model_call_count(report: dict[str, Any]) -> tuple[int, bool]:
    raw = report.get("model_call_count", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0, False
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        return 0, False
    count = int(raw)
    return (count, True) if 0 <= count <= _MAX_DATABASE_INTEGER else (0, False)


def _normalized_estimated_cost(report: dict[str, Any]) -> float | None:
    raw = report.get("estimated_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        cost = float(raw)
    except OverflowError:
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _aggregate_cost_status(priced: int, unavailable: int, *, incomplete_run_count: int = 0) -> str:
    if incomplete_run_count:
        return "unknown" if priced == 0 else "partial"
    total = priced + unavailable
    if total == 0:
        return "empty"
    if unavailable == 0:
        return "complete"
    if priced == 0:
        return "unknown"
    return "partial"


def _merge_cost_status(left: str | None, right: str) -> str:
    if left is None or left == "empty" or left == right:
        return right
    if right == "empty":
        return left
    return "partial"


def _max_severity(left: str, right: str) -> str:
    rank = {"high": 3, "medium": 2, "low": 1}
    return left if rank.get(left, 0) >= rank.get(right, 0) else right


def _priority_score(item: dict[str, Any]) -> float:
    severity_weight = {"high": 1000.0, "medium": 500.0, "low": 100.0}.get(item["severity"], 0.0)
    patch_weight = 100.0 if item["patchable_count"] else 0.0
    latency_weight = float(item["estimated_latency_savings_ms"]) / 100.0
    cost_weight = float(item["estimated_cost_savings_usd"] or 0.0) * 1000.0
    occurrence_weight = float(item["occurrence_count"]) * 25.0
    return round(
        severity_weight + patch_weight + latency_weight + cost_weight + occurrence_weight, 3
    )


def _build_optimization_queue(
    findings: list[dict[str, Any]], project_id: str | None
) -> list[dict[str, Any]]:
    """Cluster unresolved findings into a priority-ranked optimization queue.

    Within one run, findings of the same type and title that share affected
    spans are the same underlying problem reported per occurrence — summing
    their savings would double-count. Each cluster's savings therefore come
    from the compatible (span-disjoint) selection per run, summed across runs,
    and ``priority_score`` consumes those deduplicated totals.
    """
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    cluster_items: dict[tuple[str, str], dict[str, list[SavingsItem]]] = {}
    for finding in findings:
        key = (finding["type"], finding["title"])
        cluster = clusters.setdefault(
            key,
            {
                "queue_id": "queue_"
                + hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:12],
                "type": finding["type"],
                "title": finding["title"],
                "status": "detected",
                "severity": finding["severity"],
                "occurrence_count": 0,
                "run_count": 0,
                "affected_runs": [],
                "patchable_count": 0,
                "estimated_latency_savings_ms": 0.0,
                "estimated_cost_savings_usd": 0.0,
                "cost_status": None,
                "latest_created_at": finding["created_at"],
                "project_id": project_id,
                "quality_risk": _quality_risk(finding["type"]),
                "requires_scorer": _quality_risk(finding["type"]) in {"high", "medium"},
                "safe_to_auto_patch": False,
            },
        )
        cluster["occurrence_count"] += 1
        if finding["run_id"] not in cluster["affected_runs"]:
            cluster["affected_runs"].append(finding["run_id"])
            cluster["run_count"] += 1
        cluster["patchable_count"] += 1 if finding["patchable"] else 0
        cluster["severity"] = _max_severity(cluster["severity"], finding["severity"])
        cluster["latest_created_at"] = max(
            str(cluster["latest_created_at"]), str(finding["created_at"])
        )
        raw_payload = finding.get("finding")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        metadata = payload.get("metadata")
        finding_status = metadata.get("cost_status") if isinstance(metadata, dict) else None
        if finding_status not in _COST_STATUSES:
            finding_status = (
                "complete" if finding["estimated_cost_savings_usd"] is not None else "unknown"
            )
        cluster["cost_status"] = _merge_cost_status(cluster["cost_status"], finding_status)
        cluster_items.setdefault(key, {}).setdefault(finding["run_id"], []).append(
            SavingsItem(
                spans=frozenset(payload.get("affected_spans") or []),
                latency_ms=float(finding["estimated_latency_savings_ms"] or 0.0),
                cost_usd=float(finding["estimated_cost_savings_usd"] or 0.0),
            )
        )

    for key, cluster in clusters.items():
        latency = 0.0
        cost = 0.0
        for run_items in cluster_items[key].values():
            selection = select_compatible(run_items)
            latency += selection.latency_ms
            cost += selection.cost_usd
        cluster["estimated_latency_savings_ms"] = round(latency, 3)
        cluster["cost_status"] = cluster["cost_status"] or "complete"
        cluster["estimated_cost_savings_usd"] = (
            round(cost, 6) if cluster["cost_status"] in {"complete", "empty"} else None
        )

    queue = list(clusters.values())
    for item in queue:
        item["safe_to_auto_patch"] = item["patchable_count"] > 0 and item["quality_risk"] == "low"
        item["priority_score"] = _priority_score(item)
    return sorted(queue, key=lambda item: item["priority_score"], reverse=True)


def _quality_risk(finding_type: str) -> str:
    if finding_type in {"route_to_smaller_model", "split_large_step", "batch_model_calls"}:
        return "high"
    if finding_type in {
        "cache_context",
        "add_schema_validation",
        "runaway_loop",
        "tool_oscillation",
    }:
        return "medium"
    return "low"


@dataclass
class SQLiteTraceStore:
    path: str = "runs/agentloop.db"

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            apply_sqlite_migrations(conn)

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]:
        self.init()
        api_key = new_api_key()
        prefix = api_key[:_API_KEY_PREFIX_LENGTH]
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
            conn.execute(
                "INSERT INTO api_keys(key_hash, project_id, name, prefix) VALUES (?, ?, ?, ?)",
                (hash_api_key(api_key), project_id, name, prefix),
            )
        return {"api_key": api_key, "project_id": project_id, "name": name, "prefix": prefix}

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None:
        self.init()
        prefix = api_key[:_API_KEY_PREFIX_LENGTH]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_hash, project_id, name, prefix FROM api_keys WHERE prefix = ?",
                (prefix,),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if verify_api_key_hash(api_key, candidate["key_hash"])
                ),
                None,
            )
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE key_hash = ?",
                (row["key_hash"],),
            )
            return {key: row[key] for key in ("project_id", "name", "prefix")}

    def _upsert_trace(
        self, conn: sqlite3.Connection, trace: AgentTrace, project_id: str
    ) -> dict[str, Any]:
        report = trace.report()
        cost_status, priced_calls, unavailable_calls = _report_cost_completeness(report)
        model_call_count, _ = _normalized_model_call_count(report)
        estimated_cost_usd = _normalized_estimated_cost(report)
        conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
        cursor = conn.execute(
            """
            INSERT INTO traces(
                run_id, project_id, name, started_at, payload_json, total_runtime_ms,
                estimated_cost_usd, cost_status, priced_model_call_count,
                unavailable_model_call_count, model_call_count, tool_call_count, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                project_id=excluded.project_id,
                name=excluded.name,
                started_at=excluded.started_at,
                payload_json=excluded.payload_json,
                total_runtime_ms=excluded.total_runtime_ms,
                estimated_cost_usd=excluded.estimated_cost_usd,
                cost_status=excluded.cost_status,
                priced_model_call_count=excluded.priced_model_call_count,
                unavailable_model_call_count=excluded.unavailable_model_call_count,
                model_call_count=excluded.model_call_count,
                tool_call_count=excluded.tool_call_count,
                retry_count=excluded.retry_count
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
        return report

    def _upsert_usage(
        self, conn: sqlite3.Connection, project_id: str, run_id: str, report: dict[str, Any]
    ) -> None:
        cost_status, priced_calls, unavailable_calls = _report_cost_completeness(report)
        model_call_count, _ = _normalized_model_call_count(report)
        estimated_cost_usd = _normalized_estimated_cost(report)
        conn.execute(
            """
            INSERT INTO usage_events(
                project_id, run_id, total_runtime_ms, estimated_cost_usd, input_tokens,
                output_tokens, cost_status, priced_model_call_count,
                unavailable_model_call_count, model_call_count, tool_call_count, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                created_at=CURRENT_TIMESTAMP
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

    def _upsert_findings(
        self, conn: sqlite3.Connection, diagnosis: dict[str, Any], project_id: str
    ) -> None:
        conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
        run_id = diagnosis["run_id"]
        new_finding_ids: list[str] = []
        for finding in diagnosis.get("findings", []):
            savings = finding.get("savings", {})
            rewrite = finding.get("rewrite", {})
            new_finding_ids.append(finding["finding_id"])
            conn.execute(
                """
                INSERT INTO trace_findings(
                    project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, run_id, finding_id) DO UPDATE SET
                    type=excluded.type,
                    severity=excluded.severity,
                    title=excluded.title,
                    confidence=excluded.confidence,
                    patchable=excluded.patchable,
                    estimated_latency_savings_ms=excluded.estimated_latency_savings_ms,
                    estimated_cost_savings_usd=excluded.estimated_cost_savings_usd,
                    payload_json=excluded.payload_json,
                    status=CASE WHEN trace_findings.status = 'superseded' THEN 'detected'
                                ELSE trace_findings.status END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    project_id,
                    run_id,
                    finding["finding_id"],
                    finding["type"],
                    finding["severity"],
                    finding["title"],
                    finding["confidence"],
                    1 if rewrite.get("patchable") else 0,
                    savings.get("estimated_latency_savings_ms", 0),
                    savings.get("estimated_cost_savings_usd", 0),
                    json.dumps(finding),
                ),
            )
        # Findings from a prior diagnosis of this run that did not reappear are
        # superseded rather than deleted, unless a human already made a
        # terminal decision (resolved/dismissed) about them — that decision is
        # preserved regardless of what the analyzer currently detects. The
        # kept-finding-id set is arbitrary-length, so it is passed as a single
        # bound JSON array (via json_each) instead of building a variable-width
        # `IN (...)` clause, keeping the SQL text static.
        conn.execute(
            """
            UPDATE trace_findings
            SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ? AND run_id = ?
              AND status NOT IN ('resolved', 'dismissed', 'superseded')
              AND finding_id NOT IN (SELECT value FROM json_each(?))
            """,
            (project_id, run_id, json.dumps(new_finding_ids)),
        )

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self.init()
        with self._connect() as conn:
            report = self._upsert_trace(conn, trace, project_id)
            self._upsert_usage(conn, project_id, trace.run_id, report)
            self._upsert_findings(conn, build_diagnosis(trace), project_id)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, "
                    "cost_status, priced_model_call_count, unavailable_model_call_count, created_at "
                    "FROM traces WHERE project_id = ? ORDER BY created_at DESC, run_id DESC LIMIT ?",
                    (project_id, _LEGACY_LIST_SAFETY_LIMIT),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, "
                    "cost_status, priced_model_call_count, unavailable_model_call_count, created_at "
                    "FROM traces ORDER BY created_at DESC, run_id DESC LIMIT ?",
                    (_LEGACY_LIST_SAFETY_LIMIT,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_traces_page(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = _validate_page_limit(limit)
        after_created_at, after_run_id = decode_cursor(cursor) if cursor else (None, None)
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd,
                    cost_status, priced_model_call_count, unavailable_model_call_count, created_at
                FROM traces
                WHERE project_id = COALESCE(?, project_id)
                  AND (? = 0 OR (created_at, run_id) < (?, ?))
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (project_id, 1 if cursor else 0, after_created_at, after_run_id, limit + 1),
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor([last["created_at"], last["run_id"]])
        return {"items": items, "next_cursor": next_cursor}

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None:
        self.init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    "SELECT payload_json FROM traces WHERE run_id = ? AND project_id = ?",
                    (run_id, project_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload_json FROM traces WHERE run_id = ?", (run_id,)
                ).fetchone()
        if row is None:
            return None
        return AgentTrace.from_dict(json.loads(row["payload_json"]))

    def save_diagnosis(self, diagnosis: dict[str, Any], project_id: str = "default") -> None:
        self.init()
        with self._connect() as conn:
            self._upsert_findings(conn, diagnosis, project_id)

    def list_findings(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at, updated_at
                FROM trace_findings
                WHERE project_id = COALESCE(?, project_id)
                  AND status = COALESCE(?, status)
                ORDER BY
                    CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    estimated_cost_savings_usd DESC,
                    estimated_latency_savings_ms DESC,
                    updated_at DESC
                LIMIT ?
                """,
                (project_id or None, status or None, _LEGACY_LIST_SAFETY_LIMIT),
            ).fetchall()
        return [_finding_row(dict(row)) for row in rows]

    def list_findings_page(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Recency-ordered, keyset-paginated finding listing.

        Ordered by (updated_at, run_id, finding_id) DESC rather than the
        priority order list_findings() uses — priority mixes several columns
        with independent directions, which keyset pagination can't compare
        against a single cursor tuple.
        """
        limit = _validate_page_limit(limit)
        after_updated_at, after_run_id, after_finding_id = (
            decode_cursor(cursor) if cursor else (None, None, None)
        )
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at, updated_at
                FROM trace_findings
                WHERE project_id = COALESCE(?, project_id)
                  AND status = COALESCE(?, status)
                  AND (? = 0 OR (updated_at, run_id, finding_id) < (?, ?, ?))
                ORDER BY updated_at DESC, run_id DESC, finding_id DESC
                LIMIT ?
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
        items = [_finding_row(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor([last["updated_at"], last["run_id"], last["finding_id"]])
        return {"items": items, "next_cursor": next_cursor}

    def update_finding_status(
        self, project_id: str, run_id: str, finding_id: str, new_status: str
    ) -> dict[str, Any]:
        _validate_new_status(new_status)
        sources = _padded_sources(new_status)
        self.init()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE trace_findings
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND run_id = ? AND finding_id = ?
                  AND status IN (?, ?, ?)
                """,
                (new_status, project_id, run_id, finding_id, *sources),
            )
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT status FROM trace_findings WHERE project_id = ? AND run_id = ? AND finding_id = ?",
                    (project_id, run_id, finding_id),
                ).fetchone()
                if existing is None:
                    raise FindingNotFoundError(
                        f"finding {finding_id!r} not found for run {run_id!r} in project {project_id!r}"
                    )
                raise FindingTransitionError(existing["status"], new_status)
            row = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at, updated_at
                FROM trace_findings WHERE project_id = ? AND run_id = ? AND finding_id = ?
                """,
                (project_id, run_id, finding_id),
            ).fetchone()
        return _finding_row(dict(row))

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at, updated_at
                FROM trace_findings
                WHERE project_id = COALESCE(?, project_id)
                  AND status NOT IN ('resolved', 'dismissed', 'superseded')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (project_id or None, _QUEUE_MAX_FINDINGS),
            ).fetchall()
        findings = [_finding_row(dict(row)) for row in rows]
        return _build_optimization_queue(findings, project_id)

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._upsert_usage(conn, project_id, run_id, report)

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS run_count,
                    COALESCE(SUM(total_runtime_ms), 0) AS total_runtime_ms,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                    COALESCE(SUM(priced_model_call_count), 0) AS priced_model_call_count,
                    COALESCE(SUM(unavailable_model_call_count), 0) AS unavailable_model_call_count,
                    COALESCE(SUM(CASE WHEN cost_status IN ('partial', 'unknown') THEN 1 ELSE 0 END), 0) AS incomplete_cost_run_count,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(model_call_count), 0) AS model_call_count,
                    COALESCE(SUM(tool_call_count), 0) AS tool_call_count,
                    COALESCE(SUM(retry_count), 0) AS retry_count
                FROM usage_events
                WHERE project_id = COALESCE(?, project_id)
                """,
                (project_id or None,),
            ).fetchone()
        result = dict(row)
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


@dataclass
class PostgresTraceStore:
    dsn: str | None = None
    password_file: str | None = None

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Install postgres support with: uv sync --locked --extra postgres"
            ) from exc
        if self.dsn:
            return psycopg.connect(self.dsn)
        connect_kwargs: dict[str, str] = {}
        if self.password_file:
            connect_kwargs["password"] = _read_postgres_password(self.password_file)
        return psycopg.connect("", **connect_kwargs)

    def init(self) -> None:
        with self._connect() as conn:
            apply_postgres_migrations(conn)

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]:
        self.init()
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
        return {"api_key": api_key, "project_id": project_id, "name": name, "prefix": prefix}

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None:
        self.init()
        prefix = api_key[:_API_KEY_PREFIX_LENGTH]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_hash, project_id, name, prefix FROM api_keys WHERE prefix = %s",
                (prefix,),
            ).fetchall()
            row = next(
                (candidate for candidate in rows if verify_api_key_hash(api_key, candidate[0])),
                None,
            )
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s",
                (row[0],),
            )
            return {"project_id": row[1], "name": row[2], "prefix": row[3]}

    def _upsert_trace(self, conn, trace: AgentTrace, project_id: str) -> dict[str, Any]:
        report = trace.report()
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
        return report

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
        self.init()
        with self._connect() as conn:
            report = self._upsert_trace(conn, trace, project_id)
            self._upsert_usage(conn, project_id, trace.run_id, report)
            self._upsert_findings(conn, build_diagnosis(trace), project_id)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
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
    ) -> dict[str, Any]:
        limit = _validate_page_limit(limit)
        after_created_at, after_run_id = decode_cursor(cursor) if cursor else (None, None)
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
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd,
                    cost_status, priced_model_call_count, unavailable_model_call_count, created_at::text
                FROM traces
                WHERE project_id = COALESCE(%s, project_id)
                  AND (%s = 0 OR (created_at, run_id) < (%s, %s))
                ORDER BY created_at DESC, run_id DESC
                LIMIT %s
                """,
                (project_id, 1 if cursor else 0, after_created_at, after_run_id, limit + 1),
            ).fetchall()
        items = [dict(zip(keys, row, strict=True)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor([last["created_at"], last["run_id"]])
        return {"items": items, "next_cursor": next_cursor}

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None:
        self.init()
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
        self.init()
        with self._connect() as conn:
            self._upsert_findings(conn, diagnosis, project_id)

    def list_findings(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, run_id, finding_id, type, severity, title, confidence,
                    patchable, status, estimated_latency_savings_ms, estimated_cost_savings_usd,
                    payload_json, created_at::text, updated_at::text
                FROM trace_findings
                WHERE project_id = COALESCE(%s, project_id)
                  AND status = COALESCE(%s, status)
                ORDER BY
                    CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    estimated_cost_savings_usd DESC,
                    estimated_latency_savings_ms DESC,
                    updated_at DESC
                LIMIT %s
                """,
                (project_id or None, status or None, _LEGACY_LIST_SAFETY_LIMIT),
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

    def list_findings_page(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = _validate_page_limit(limit)
        after_updated_at, after_run_id, after_finding_id = (
            decode_cursor(cursor) if cursor else (None, None, None)
        )
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
        self.init()
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
        self.init()
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
        self.init()
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
        self.init()
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


def get_store() -> TraceStore:
    backend = os.getenv("AGENTLOOP_STORE_BACKEND", "sqlite").lower()
    if backend == "postgres":
        dsn = get_postgres_dsn()
        if not postgres_connection_source():
            raise RuntimeError(
                "AGENTLOOP_STORE_BACKEND=postgres requires AGENTLOOP_DATABASE_URL, "
                "DATABASE_URL, or libpq PGHOST/PGSERVICE configuration"
            )
        return PostgresTraceStore(
            dsn=dsn,
            password_file=None if dsn else get_postgres_password_file(),
        )
    return SQLiteTraceStore(path=os.getenv("AGENTLOOP_SQLITE_PATH", "runs/agentloop.db"))


def _read_postgres_password(path: str) -> str:
    """Read a file-backed Postgres password without including it in errors or logs."""
    try:
        password = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Unable to read Postgres password file: {path}") from exc
    if not password:
        raise RuntimeError(f"Postgres password file is empty: {path}")
    return password
