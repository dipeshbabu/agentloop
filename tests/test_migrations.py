"""Migration-system specific coverage that doesn't fit the per-row backend
contract suite in test_store_contract.py: schema equivalence between a fresh
install and an upgraded pre-#22 database, and connection-failure behavior.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from agentloop.migrations import _BASELINE_POSTGRES, _BASELINE_SQLITE, MigrationError
from agentloop.store import PostgresTraceStore, SQLiteTraceStore
from tests.test_store_contract import _POSTGRES_AVAILABLE, POSTGRES_TEST_DSN

_WHITESPACE = re.compile(r"\s+")


def _normalize(sql: str) -> str:
    return _WHITESPACE.sub(" ", sql).strip()


def _sqlite_schema_signature(path: str) -> set[tuple[str, str, str]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations'"
        ).fetchall()
    return {(kind, name, _normalize(sql or "")) for kind, name, sql in rows}


def test_sqlite_fresh_install_and_upgraded_database_reach_equivalent_schema(tmp_path):
    fresh_path = tmp_path / "fresh.db"
    SQLiteTraceStore(path=str(fresh_path)).init()

    upgraded_path = tmp_path / "upgraded.db"
    with sqlite3.connect(str(upgraded_path)) as conn:
        for statement in _BASELINE_SQLITE:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO traces(run_id, project_id, name, payload_json, model_call_count) "
            "VALUES ('legacy', 'default', 'legacy', '{}', 2)"
        )
        conn.execute(
            "INSERT INTO usage_events(project_id, run_id, model_call_count) "
            "VALUES ('default', 'legacy', 2)"
        )
    upgraded_store = SQLiteTraceStore(path=str(upgraded_path))
    upgraded_store.init()

    assert _sqlite_schema_signature(str(fresh_path)) == _sqlite_schema_signature(str(upgraded_path))

    with sqlite3.connect(str(upgraded_path)) as conn:
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        trace_cost = conn.execute(
            "SELECT cost_status, priced_model_call_count, unavailable_model_call_count "
            "FROM traces WHERE run_id = 'legacy'"
        ).fetchone()
        usage_cost = conn.execute(
            "SELECT cost_status, priced_model_call_count, unavailable_model_call_count "
            "FROM usage_events WHERE run_id = 'legacy'"
        ).fetchone()
    assert versions == {1, 2, 3, 4}
    assert trace_cost == ("complete", 2, 0)
    assert usage_cost == ("complete", 2, 0)


def test_sqlite_connection_failure_raises_a_clear_error(tmp_path):
    # Point the "database" at a path whose parent is a file, not a directory:
    # Path.mkdir(parents=True) cannot create it, so _connect() must fail loudly.
    blocked = tmp_path / "not_a_directory"
    blocked.write_text("occupied")
    store = SQLiteTraceStore(path=str(blocked / "agentloop.db"))

    with pytest.raises((OSError, sqlite3.OperationalError)):
        store.init()


pytestmark_postgres = pytest.mark.skipif(
    not _POSTGRES_AVAILABLE,
    reason=f"no Postgres service reachable at {POSTGRES_TEST_DSN}",
)


@pytestmark_postgres
def test_postgres_fresh_install_and_upgraded_database_reach_equivalent_schema():
    import psycopg

    fresh_schema = "agentloop_test_fresh_schema"
    upgraded_schema = "agentloop_test_upgraded_schema"
    with psycopg.connect(POSTGRES_TEST_DSN, autocommit=True) as conn:
        for schema in (fresh_schema, upgraded_schema):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.execute(f"CREATE SCHEMA {schema}")

    def _scoped_dsn(schema: str) -> str:
        return POSTGRES_TEST_DSN + f"?options=-c%20search_path%3D{schema}"

    try:
        PostgresTraceStore(dsn=_scoped_dsn(fresh_schema)).init()

        with psycopg.connect(_scoped_dsn(upgraded_schema)) as conn:
            for statement in _BASELINE_POSTGRES:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO traces(run_id, project_id, name, payload_json, model_call_count) "
                "VALUES ('legacy', 'default', 'legacy', '{}'::jsonb, 2)"
            )
            conn.execute(
                "INSERT INTO usage_events(project_id, run_id, model_call_count) "
                "VALUES ('default', 'legacy', 2)"
            )
            conn.commit()
        PostgresTraceStore(dsn=_scoped_dsn(upgraded_schema)).init()

        with psycopg.connect(POSTGRES_TEST_DSN) as conn:

            def _signature(schema: str) -> tuple[set[str], set[str], set[tuple[str, str]]]:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                        (schema,),
                    ).fetchall()
                    if row[0] != "schema_migrations"
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,)
                    ).fetchall()
                }
                columns = {
                    (row[0], row[1])
                    for row in conn.execute(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = %s",
                        (schema,),
                    ).fetchall()
                    if row[0] != "schema_migrations"
                }
                return tables, indexes, columns

            fresh_tables, fresh_indexes, fresh_columns = _signature(fresh_schema)
            upgraded_tables, upgraded_indexes, upgraded_columns = _signature(upgraded_schema)

        assert fresh_tables == upgraded_tables
        assert fresh_indexes == upgraded_indexes
        assert fresh_columns == upgraded_columns
        assert ("traces", "cost_status") in fresh_columns
        assert ("usage_events", "unavailable_model_call_count") in fresh_columns

        with psycopg.connect(_scoped_dsn(upgraded_schema)) as conn:
            versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            trace_cost = conn.execute(
                "SELECT cost_status, priced_model_call_count, unavailable_model_call_count "
                "FROM traces WHERE run_id = 'legacy'"
            ).fetchone()
            usage_cost = conn.execute(
                "SELECT cost_status, priced_model_call_count, unavailable_model_call_count "
                "FROM usage_events WHERE run_id = 'legacy'"
            ).fetchone()
        assert versions == {1, 2, 3, 4}
        assert trace_cost == ("complete", 2, 0)
        assert usage_cost == ("complete", 2, 0)
    finally:
        with psycopg.connect(POSTGRES_TEST_DSN, autocommit=True) as conn:
            for schema in (fresh_schema, upgraded_schema):
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytestmark_postgres
def test_postgres_connection_failure_raises_a_clear_error():
    import psycopg

    bad_dsn = "postgresql://nonexistent_user:wrong@localhost:1/does_not_exist"
    store = PostgresTraceStore(dsn=bad_dsn)
    with pytest.raises(psycopg.OperationalError):
        store.init()


def test_migration_error_message_names_the_failed_migration(tmp_path, monkeypatch):
    from agentloop import migrations as migrations_module

    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    store.init()  # baseline is already applied; force migration 2 to fail

    broken = migrations_module.Migration(
        version=99,
        name="broken_for_test",
        sqlite_statements=("SELECT this is not valid sql",),
        postgres_statements=(),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (*migrations_module.MIGRATIONS, broken))

    with pytest.raises(MigrationError, match="broken_for_test"):
        store.init()
