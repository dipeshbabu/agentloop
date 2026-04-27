from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentloop.tracer import AgentTrace


class TraceStore(Protocol):
    def init(self) -> None: ...

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]: ...

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None: ...

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None: ...

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None: ...

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None: ...

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]: ...


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "al_" + secrets.token_urlsafe(32)


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
                """
            )
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", ("default",))

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]:
        self.init()
        api_key = new_api_key()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
            conn.execute(
                "INSERT INTO api_keys(key_hash, project_id, name, prefix) VALUES (?, ?, ?, ?)",
                (hash_api_key(api_key), project_id, name, api_key[:10]),
            )
        return {"api_key": api_key, "project_id": project_id, "name": name, "prefix": api_key[:10]}

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT project_id, name, prefix FROM api_keys WHERE key_hash = ?", (hash_api_key(api_key),)
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE key_hash = ?", (hash_api_key(api_key),))
            return dict(row)

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self.init()
        report = trace.report()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO projects(project_id) VALUES (?)", (project_id,))
            conn.execute(
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
        self.record_usage(project_id, trace.run_id, report)

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
                    "SELECT payload_json FROM traces WHERE run_id = ? AND project_id = ?", (run_id, project_id)
                ).fetchone()
            else:
                row = conn.execute("SELECT payload_json FROM traces WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return AgentTrace.from_dict(json.loads(row["payload_json"]))

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
        where = "WHERE project_id = ?" if project_id else ""
        args = (project_id,) if project_id else ()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS run_count,
                    COALESCE(SUM(total_runtime_ms), 0) AS total_runtime_ms,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(model_call_count), 0) AS model_call_count,
                    COALESCE(SUM(tool_call_count), 0) AS tool_call_count,
                    COALESCE(SUM(retry_count), 0) AS retry_count
                FROM usage_events {where}
                """,
                args,
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
            raise RuntimeError("Install postgres support with: pip install -e '.[postgres]'") from exc
        return psycopg.connect(self.dsn)

    def init(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())")
            conn.execute("CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), name TEXT NOT NULL, prefix TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now(), last_used_at TIMESTAMPTZ)")
            conn.execute("CREATE TABLE IF NOT EXISTS traces (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), name TEXT NOT NULL, started_at TEXT, payload_json JSONB NOT NULL, total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())")
            conn.execute("CREATE TABLE IF NOT EXISTS usage_events (id BIGSERIAL PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL, total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())")
            conn.execute("INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", ("default",))

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]:
        self.init()
        api_key = new_api_key()
        with self._connect() as conn:
            conn.execute("INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,))
            conn.execute(
                "INSERT INTO api_keys(key_hash, project_id, name, prefix) VALUES (%s, %s, %s, %s)",
                (hash_api_key(api_key), project_id, name, api_key[:10]),
            )
        return {"api_key": api_key, "project_id": project_id, "name": name, "prefix": api_key[:10]}

    def verify_api_key(self, api_key: str) -> dict[str, Any] | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT project_id, name, prefix FROM api_keys WHERE key_hash = %s", (hash_api_key(api_key),)
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s", (hash_api_key(api_key),))
            return {"project_id": row[0], "name": row[1], "prefix": row[2]}

    def save_trace(self, trace: AgentTrace, project_id: str = "default") -> None:
        self.init()
        report = trace.report()
        with self._connect() as conn:
            conn.execute("INSERT INTO projects(project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project_id,))
            conn.execute(
                """
                INSERT INTO traces(run_id, project_id, name, started_at, payload_json, total_runtime_ms, estimated_cost_usd, model_call_count, tool_call_count, retry_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(run_id) DO UPDATE SET project_id=excluded.project_id, name=excluded.name, started_at=excluded.started_at,
                payload_json=excluded.payload_json, total_runtime_ms=excluded.total_runtime_ms, estimated_cost_usd=excluded.estimated_cost_usd,
                model_call_count=excluded.model_call_count, tool_call_count=excluded.tool_call_count, retry_count=excluded.retry_count
                """,
                (trace.run_id, project_id, trace.name, trace.started_at, json.dumps(trace.to_dict()), report.get("total_runtime_ms", 0), report.get("estimated_cost_usd", 0), report.get("model_call_count", 0), report.get("tool_call_count", 0), report.get("retry_count", 0)),
            )
        self.record_usage(project_id, trace.run_id, report)

    def list_traces(self, project_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect() as conn:
            if project_id:
                rows = conn.execute("SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at::text FROM traces WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT run_id, project_id, name, started_at, total_runtime_ms, estimated_cost_usd, created_at::text FROM traces ORDER BY created_at DESC").fetchall()
        keys = ["run_id", "project_id", "name", "started_at", "total_runtime_ms", "estimated_cost_usd", "created_at"]
        return [dict(zip(keys, row)) for row in rows]

    def get_trace(self, run_id: str, project_id: str | None = None) -> AgentTrace | None:
        self.init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute("SELECT payload_json FROM traces WHERE run_id = %s AND project_id = %s", (run_id, project_id)).fetchone()
            else:
                row = conn.execute("SELECT payload_json FROM traces WHERE run_id = %s", (run_id,)).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return AgentTrace.from_dict(payload)

    def record_usage(self, project_id: str, run_id: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_events(project_id, run_id, total_runtime_ms, estimated_cost_usd, input_tokens, output_tokens, model_call_count, tool_call_count, retry_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (project_id, run_id, report.get("total_runtime_ms", 0), report.get("estimated_cost_usd", 0), report.get("input_tokens", 0), report.get("output_tokens", 0), report.get("model_call_count", 0), report.get("tool_call_count", 0), report.get("retry_count", 0)),
            )

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        self.init()
        with self._connect() as conn:
            if project_id:
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events WHERE project_id = %s", (project_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_runtime_ms),0), COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_call_count),0), COALESCE(SUM(tool_call_count),0), COALESCE(SUM(retry_count),0) FROM usage_events").fetchone()
        keys = ["run_count", "total_runtime_ms", "estimated_cost_usd", "input_tokens", "output_tokens", "model_call_count", "tool_call_count", "retry_count"]
        result = dict(zip(keys, row))
        if project_id:
            result["project_id"] = project_id
        return result


def get_store() -> TraceStore:
    backend = os.getenv("AGENTLOOP_STORE_BACKEND", "sqlite").lower()
    if backend == "postgres":
        dsn = os.getenv("AGENTLOOP_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("AGENTLOOP_STORE_BACKEND=postgres requires AGENTLOOP_DATABASE_URL or DATABASE_URL")
        return PostgresTraceStore(dsn=dsn)
    return SQLiteTraceStore(path=os.getenv("AGENTLOOP_SQLITE_PATH", "runs/agentloop.db"))
