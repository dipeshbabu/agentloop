"""Versioned schema migrations shared by :mod:`agentloop.store`'s two backends.

Each :class:`Migration` carries dialect-specific, idempotent SQL statements.
Idempotency (``IF NOT EXISTS`` / ``ON CONFLICT ... DO NOTHING``) lets concurrent
``init()`` calls apply the same pending migration twice without corrupting state
or racing on `schema_migrations` bookkeeping, which stands in for a real
distributed lock that neither backend otherwise provides here.

A fresh install and an existing pre-migration database both start with no rows
in `schema_migrations`; migration 0001 reproduces the exact schema `init()`
used to create inline, so both converge on the same structure.
"""

from __future__ import annotations

from dataclasses import dataclass


class MigrationError(RuntimeError):
    """Raised when a migration statement fails to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_statements: tuple[str, ...]
    postgres_statements: tuple[str, ...]


_BASELINE_SQLITE = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key_hash TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT,
        FOREIGN KEY(project_id) REFERENCES projects(project_id)
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    "INSERT OR IGNORE INTO projects(project_id) VALUES ('default')",
)

_BASELINE_POSTGRES = (
    "CREATE TABLE IF NOT EXISTS projects ("
    "project_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS api_keys ("
    "key_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), "
    "name TEXT NOT NULL, prefix TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now(), "
    "last_used_at TIMESTAMPTZ)",
    "CREATE TABLE IF NOT EXISTS traces ("
    "run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), "
    "name TEXT NOT NULL, started_at TEXT, payload_json JSONB NOT NULL, "
    "total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, "
    "model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, "
    "retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS usage_events ("
    "id BIGSERIAL PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL, "
    "total_runtime_ms DOUBLE PRECISION DEFAULT 0, estimated_cost_usd DOUBLE PRECISION DEFAULT 0, "
    "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, "
    "model_call_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, "
    "retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS trace_findings ("
    "project_id TEXT NOT NULL REFERENCES projects(project_id), "
    "run_id TEXT NOT NULL REFERENCES traces(run_id), finding_id TEXT NOT NULL, "
    "type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, confidence TEXT NOT NULL, "
    "patchable BOOLEAN DEFAULT false, status TEXT DEFAULT 'detected', "
    "estimated_latency_savings_ms DOUBLE PRECISION DEFAULT 0, "
    "estimated_cost_savings_usd DOUBLE PRECISION DEFAULT 0, payload_json JSONB NOT NULL, "
    "created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), "
    "PRIMARY KEY(project_id, run_id, finding_id))",
    "INSERT INTO projects(project_id) VALUES ('default') ON CONFLICT DO NOTHING",
)

# usage_events had no uniqueness constraint, so retried save_trace() calls appended
# a new row per retry instead of updating one. De-duplicate existing data (keep the
# most recent row per project/run) before the constraint can be added, or the
# index creation fails on any store that already has duplicates.
_DEDUPE_AND_CONSTRAIN_USAGE_EVENTS = (
    "DELETE FROM usage_events WHERE id NOT IN "
    "(SELECT MAX(id) FROM usage_events GROUP BY project_id, run_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_usage_events_project_run "
    "ON usage_events(project_id, run_id)",
)

_PAGINATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_traces_project_created "
    "ON traces(project_id, created_at, run_id)",
    "CREATE INDEX IF NOT EXISTS ix_trace_findings_project_status_updated "
    "ON trace_findings(project_id, status, updated_at, run_id, finding_id)",
)

_COST_COMPLETENESS_SQLITE = (
    "ALTER TABLE traces ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'complete'",
    "ALTER TABLE traces ADD COLUMN priced_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE traces ADD COLUMN unavailable_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'complete'",
    "ALTER TABLE usage_events ADD COLUMN priced_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN unavailable_model_call_count INTEGER NOT NULL DEFAULT 0",
    "UPDATE traces SET cost_status = CASE WHEN model_call_count = 0 THEN 'empty' ELSE 'complete' END, "
    "priced_model_call_count = model_call_count, unavailable_model_call_count = 0",
    "UPDATE usage_events SET cost_status = CASE WHEN model_call_count = 0 THEN 'empty' ELSE 'complete' END, "
    "priced_model_call_count = model_call_count, unavailable_model_call_count = 0",
)

_COST_COMPLETENESS_POSTGRES = (
    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS cost_status TEXT NOT NULL DEFAULT 'complete'",
    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS priced_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS unavailable_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_status TEXT NOT NULL DEFAULT 'complete'",
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS priced_model_call_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS unavailable_model_call_count INTEGER NOT NULL DEFAULT 0",
    "UPDATE traces SET cost_status = CASE WHEN model_call_count = 0 THEN 'empty' ELSE 'complete' END, "
    "priced_model_call_count = model_call_count, unavailable_model_call_count = 0",
    "UPDATE usage_events SET cost_status = CASE WHEN model_call_count = 0 THEN 'empty' ELSE 'complete' END, "
    "priced_model_call_count = model_call_count, unavailable_model_call_count = 0",
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline",
        sqlite_statements=_BASELINE_SQLITE,
        postgres_statements=_BASELINE_POSTGRES,
    ),
    Migration(
        version=2,
        name="idempotent_usage_events",
        sqlite_statements=_DEDUPE_AND_CONSTRAIN_USAGE_EVENTS,
        postgres_statements=_DEDUPE_AND_CONSTRAIN_USAGE_EVENTS,
    ),
    Migration(
        version=3,
        name="pagination_indexes",
        sqlite_statements=_PAGINATION_INDEXES,
        postgres_statements=_PAGINATION_INDEXES,
    ),
    Migration(
        version=4,
        name="cost_completeness",
        sqlite_statements=_COST_COMPLETENESS_SQLITE,
        postgres_statements=_COST_COMPLETENESS_POSTGRES,
    ),
)


def _ensure_migrations_table_sqlite(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _ensure_migrations_table_postgres(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ DEFAULT now())"
    )


def apply_sqlite_migrations(conn) -> None:
    """Apply pending migrations to a SQLite connection inside its open transaction."""
    import sqlite3

    _ensure_migrations_table_sqlite(conn)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        try:
            for statement in migration.sqlite_statements:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    # SQLite has no `ADD COLUMN IF NOT EXISTS`. Concurrent
                    # initializers can both observe migration 4 as pending;
                    # after one commits the column, the other receives this
                    # narrow duplicate-column error. Treat only that verified
                    # race as idempotent and preserve every other SQL failure.
                    if not (
                        statement.lstrip().upper().startswith("ALTER TABLE")
                        and "duplicate column name" in str(exc).lower()
                    ):
                        raise
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed: {exc}. "
                "The transaction was rolled back; the store is unchanged. Fix the "
                "underlying issue (e.g. free disk space, resolve a lock) and retry."
            ) from exc


def apply_postgres_migrations(conn) -> None:
    """Apply pending migrations to a psycopg connection inside its open transaction."""
    import psycopg

    _ensure_migrations_table_postgres(conn)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        try:
            for statement in migration.postgres_statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (%s, %s) "
                "ON CONFLICT (version) DO NOTHING",
                (migration.version, migration.name),
            )
        except psycopg.Error as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed: {exc}. "
                "The transaction was rolled back; the store is unchanged. Fix the "
                "underlying issue (e.g. permissions, a blocking lock) and retry."
            ) from exc
