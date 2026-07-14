from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentloop.findings import build_diagnosis
from agentloop.tracer import AgentTrace

_API_KEY_PREFIX_LENGTH = 10
_API_KEY_SALT_BYTES = 16
_API_KEY_HASH_BYTES = 32
_API_KEY_SCRYPT_N = 2**14
_API_KEY_SCRYPT_R = 8
_API_KEY_SCRYPT_P = 1


class TraceProjectConflictError(ValueError):
    """Raised when a run ID is already owned by a different project."""


class TraceStore(Protocol):
    def init(self) -> None: ...

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]: ...

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None: ...

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None: ...

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None: ...

    def save_diagnosis(self, diagnosis: dict[str, Any], project_id: str = "default") -> None: ...

    def list_findings(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]: ...

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


def _finding_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_payload = row.pop("payload_json")
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    row["patchable"] = bool(row["patchable"])
    row["estimated_latency_savings_ms"] = float(row["estimated_latency_savings_ms"] or 0)
    row["estimated_cost_savings_usd"] = float(row["estimated_cost_savings_usd"] or 0)
    row["finding"] = payload
    return row


def _max_severity(left: str, right: str) -> str:
    rank = {"high": 3, "medium": 2, "low": 1}
    return left if rank.get(left, 0) >= rank.get(right, 0) else right


def _priority_score(item: dict[str, Any]) -> float:
    severity_weight = {"high": 1000.0, "medium": 500.0, "low": 100.0}.get(item["severity"], 0.0)
    patch_weight = 100.0 if item["patchable_count"] else 0.0
    latency_weight = float(item["estimated_latency_savings_ms"]) / 100.0
    cost_weight = float(item["estimated_cost_savings_usd"]) * 1000.0
    occurrence_weight = float(item["occurrence_count"]) * 25.0
    return round(
        severity_weight + patch_weight + latency_weight + cost_weight + occurrence_weight, 3
    )


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
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS traces (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    started_at TEXT,
                    payload_json TEXT NOT NULL,
                    total_runtime_ms REAL DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0,
                    model_call_count INTEGER DEFAULT 0,
                    tool_call_count INTEGER DEFAULT 0,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    total_runtime_ms REAL DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    model_call_count INTEGER DEFAULT 0,
                    tool_call_count INTEGER DEFAULT 0,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trace_findings (
                    project_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    patchable INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'detected',
                    estimated_latency_savings_ms REAL DEFAULT 0,
                    estimated_cost_savings_usd REAL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id, run_id, finding_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(run_id) REFERENCES traces(run_id)
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", ("default",))

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

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self.init()
        report = trace.report()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
            cursor = conn.execute(
                """
                INSERT INTO traces(
                    run_id, project_id, name, started_at, payload_json, total_runtime_ms,
                    estimated_cost_usd, model_call_count, tool_call_count, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    name=excluded.name,
                    started_at=excluded.started_at,
                    payload_json=excluded.payload_json,
                    total_runtime_ms=excluded.total_runtime_ms,
                    estimated_cost_usd=excluded.estimated_cost_usd,
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
                    report.get("estimated_cost_usd", 0),
                    report.get("model_call_count", 0),
                    report.get("tool_call_count", 0),
                    report.get("retry_count", 0),
                ),
            )
            if cursor.rowcount == 0:
                raise TraceProjectConflictError(
                    f"run_id {trace.run_id!r} already belongs to another project"
                )
        self.record_usage(project_id, trace.run_id, report)
        self.save_diagnosis(build_diagnosis(trace), project_id=project_id)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at FROM traces WHERE project_id = ? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at FROM traces ORDER BY created_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

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
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
            conn.execute(
                "DELETE FROM trace_findings WHERE project_id = ? AND run_id = ?",
                (project_id, diagnosis["run_id"]),
            )
            for finding in diagnosis.get("findings", []):
                savings = finding.get("savings", {})
                rewrite = finding.get("rewrite", {})
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
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        project_id,
                        diagnosis["run_id"],
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
                """,
                (project_id or None, status or None),
            ).fetchall()
        return [_finding_row(dict(row)) for row in rows]

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]:
        findings = [
            finding
            for finding in self.list_findings(project_id=project_id)
            if finding["status"] != "resolved"
        ]
        clusters: dict[tuple[str, str], dict[str, Any]] = {}
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
            cluster["estimated_latency_savings_ms"] += finding["estimated_latency_savings_ms"]
            cluster["estimated_cost_savings_usd"] += finding["estimated_cost_savings_usd"]
            cluster["severity"] = _max_severity(cluster["severity"], finding["severity"])
            cluster["latest_created_at"] = max(
                str(cluster["latest_created_at"]), str(finding["created_at"])
            )

        queue = list(clusters.values())
        for item in queue:
            item["estimated_latency_savings_ms"] = round(item["estimated_latency_savings_ms"], 3)
            item["estimated_cost_savings_usd"] = round(item["estimated_cost_savings_usd"], 6)
            item["safe_to_auto_patch"] = (
                item["patchable_count"] > 0 and item["quality_risk"] == "low"
            )
            item["priority_score"] = _priority_score(item)
        return sorted(queue, key=lambda item: item["priority_score"], reverse=True)

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events(
                    project_id, run_id, total_runtime_ms, estimated_cost_usd, input_tokens,
                    output_tokens, model_call_count, tool_call_count, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    run_id,
                    report.get("total_runtime_ms", 0),
                    report.get("estimated_cost_usd", 0),
                    report.get("input_tokens", 0),
                    report.get("output_tokens", 0),
                    report.get("model_call_count", 0),
                    report.get("tool_call_count", 0),
                    report.get("retry_count", 0),
                ),
            )

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS run_count,
                    COALESCE(SUM(total_runtime_ms), 0) AS total_runtime_ms,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
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
        if project_id:
            result["project_id"] = project_id
        return result


@dataclass
class PostgresTraceStore:
    dsn: str

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Install postgres support with: uv sync --locked --extra postgres"
            ) from exc
        return psycopg.connect(self.dsn)

    def init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), name TEXT NOT NULL, prefix TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now(), last_used_at TIMESTAMPTZ)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS traces (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), name TEXT NOT NULL, started_at TEXT, payload_json JSONB NOT NULL, total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS usage_events (id BIGSERIAL PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL, total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trace_findings (project_id TEXT NOT NULL REFERENCES projects(project_id), run_id TEXT NOT NULL REFERENCES traces(run_id), finding_id TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, confidence TEXT NOT NULL, patchable BOOLEAN DEFAULT false, status TEXT DEFAULT 'detected', estimated_latency_savings_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_savings_usd DOUBLE PRECISION DEFAULT 0, payload_json JSONB NOT NULL, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(project_id, run_id, finding_id))"
            )
            conn.execute(
                "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", ("default",)
            )

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

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self.init()
        report = trace.report()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,)
            )
            cursor = conn.execute(
                """
                INSERT INTO traces(run_id, project_id, name, started_at, payload_json, total_runtime_ms, estimated_cost_usd, model_call_count, tool_call_count, retry_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(run_id) DO UPDATE SET project_id=excluded.project_id, name=excluded.name, started_at=excluded.started_at,
                payload_json=excluded.payload_json, total_runtime_ms=excluded.total_runtime_ms, estimated_cost_usd=excluded.estimated_cost_usd,
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
                    report.get("estimated_cost_usd", 0),
                    report.get("model_call_count", 0),
                    report.get("tool_call_count", 0),
                    report.get("retry_count", 0),
                ),
            )
            if cursor.rowcount == 0:
                raise TraceProjectConflictError(
                    f"run_id {trace.run_id!r} already belongs to another project"
                )
        self.record_usage(project_id, trace.run_id, report)
        self.save_diagnosis(build_diagnosis(trace), project_id=project_id)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at::text FROM traces WHERE project_id = %s ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at::text FROM traces ORDER BY created_at DESC"
                ).fetchall()
        keys = [
            "run_id",
            "project_id",
            "name",
            "started_at",
            "total_runtime_ms",
            "estimated_cost_usd",
            "created_at",
        ]
        return [dict(zip(keys, row)) for row in rows]

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
            conn.execute(
                "INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,)
            )
            conn.execute(
                "DELETE FROM trace_findings WHERE project_id = %s AND run_id = %s",
                (project_id, diagnosis["run_id"]),
            )
            for finding in diagnosis.get("findings", []):
                savings = finding.get("savings", {})
                rewrite = finding.get("rewrite", {})
                conn.execute(
                    """
                    INSERT INTO trace_findings(project_id, run_id, finding_id, type, severity, title, confidence, patchable, estimated_latency_savings_ms, estimated_cost_savings_usd, payload_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(project_id, run_id, finding_id) DO UPDATE SET
                    type=excluded.type, severity=excluded.severity, title=excluded.title, confidence=excluded.confidence,
                    patchable=excluded.patchable, estimated_latency_savings_ms=excluded.estimated_latency_savings_ms,
                    estimated_cost_savings_usd=excluded.estimated_cost_savings_usd, payload_json=excluded.payload_json,
                    updated_at=now()
                    """,
                    (
                        project_id,
                        diagnosis["run_id"],
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
                """,
                (project_id or None, status or None),
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
        return [_finding_row(dict(zip(keys, row))) for row in rows]

    def optimization_queue(self, project_id: str | None = None) -> list[dict[str, Any]]:
        findings = [
            finding
            for finding in self.list_findings(project_id=project_id)
            if finding["status"] != "resolved"
        ]
        clusters: dict[tuple[str, str], dict[str, Any]] = {}
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
            cluster["estimated_latency_savings_ms"] += finding["estimated_latency_savings_ms"]
            cluster["estimated_cost_savings_usd"] += finding["estimated_cost_savings_usd"]
            cluster["severity"] = _max_severity(cluster["severity"], finding["severity"])
            cluster["latest_created_at"] = max(
                str(cluster["latest_created_at"]), str(finding["created_at"])
            )
        queue = list(clusters.values())
        for item in queue:
            item["estimated_latency_savings_ms"] = round(item["estimated_latency_savings_ms"], 3)
            item["estimated_cost_savings_usd"] = round(item["estimated_cost_savings_usd"], 6)
            item["safe_to_auto_patch"] = (
                item["patchable_count"] > 0 and item["quality_risk"] == "low"
            )
            item["priority_score"] = _priority_score(item)
        return sorted(queue, key=lambda item: item["priority_score"], reverse=True)

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_events(project_id, run_id, total_runtime_ms, estimated_cost_usd, input_tokens, output_tokens, model_call_count, tool_call_count, retry_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    run_id,
                    report.get("total_runtime_ms", 0),
                    report.get("estimated_cost_usd", 0),
                    report.get("input_tokens", 0),
                    report.get("output_tokens", 0),
                    report.get("model_call_count", 0),
                    report.get("tool_call_count", 0),
                    report.get("retry_count", 0),
                ),
            )

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        self.init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events"
                ).fetchone()
        keys = [
            "run_count",
            "total_runtime_ms",
            "estimated_cost_usd",
            "input_tokens",
            "output_tokens",
            "model_call_count",
            "tool_call_count",
            "retry_count",
        ]
        result = dict(zip(keys, row))
        if project_id:
            result["project_id"] = project_id
        return result


def get_store() -> TraceStore:
    backend = os.getenv("AGENTLOOP_STORE_BACKEND", "sqlite").lower()
    if backend == "postgres":
        dsn = os.getenv("AGENTLOOP_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "AGENTLOOP_STORE_BACKEND=postgres requires AGENTLOOP_DATABASE_URL or DATABASE_URL"
            )
        return PostgresTraceStore(dsn=dsn)
    return SQLiteTraceStore(path=os.getenv("AGENTLOOP_SQLITE_PATH", "runs/agentloop.db"))
