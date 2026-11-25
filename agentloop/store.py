"""Trace storage: SQLite and Postgres backends behind one interface.

The implementation lives in ``store_types``, ``store_common``, ``store_sqlite``,
``store_postgres`` and ``store_keys`` (issue #407). Every public name, and the
private helpers other modules and tests use, is re-exported here, so
``from agentloop.store import ...`` keeps working unchanged.
"""

from __future__ import annotations

import os

from agentloop.config import (
    get_postgres_dsn,
    get_postgres_password_file,
    postgres_connection_source,
)
from agentloop.cursor_validation import InvalidCursorError, decode_cursor, encode_cursor
from agentloop.migrations import apply_postgres_migrations, apply_sqlite_migrations
from agentloop.store_common import (
    LABEL_KEY_PATTERN,
    MAX_LABEL_VALUE_LENGTH,
    parse_label_filter,
    validate_label_filter,
)
from agentloop.store_common import _build_optimization_queue as _build_optimization_queue
from agentloop.store_common import _find_api_key_row as _find_api_key_row
from agentloop.store_keys import hash_api_key, new_api_key, verify_api_key_hash
from agentloop.store_postgres import PostgresTraceStore
from agentloop.store_postgres import _read_postgres_password as _read_postgres_password
from agentloop.store_sqlite import SQLiteTraceStore
from agentloop.store_types import (
    ALLOWED_FINDING_TRANSITIONS,
    DEFAULT_PAGE_SIZE,
    FINDING_STATUSES,
    MAX_PAGE_SIZE,
    FindingNotFoundError,
    FindingTransitionError,
    TraceProjectConflictError,
    TraceStore,
)

__all__ = [
    "ALLOWED_FINDING_TRANSITIONS",
    "DEFAULT_PAGE_SIZE",
    "FINDING_STATUSES",
    "LABEL_KEY_PATTERN",
    "MAX_LABEL_VALUE_LENGTH",
    "MAX_PAGE_SIZE",
    "FindingNotFoundError",
    "FindingTransitionError",
    "InvalidCursorError",
    "PostgresTraceStore",
    "SQLiteTraceStore",
    "TraceProjectConflictError",
    "TraceStore",
    "apply_postgres_migrations",
    "apply_sqlite_migrations",
    "decode_cursor",
    "encode_cursor",
    "get_store",
    "hash_api_key",
    "new_api_key",
    "parse_label_filter",
    "validate_label_filter",
    "verify_api_key_hash",
]


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
