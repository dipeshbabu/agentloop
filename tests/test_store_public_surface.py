"""Contract for agentloop.store's names while it is split into modules (#407).

Code outside AgentLoop imports these names from ``agentloop.store``. Moving an
implementation to another module must keep every one importable from here,
as the same object, so the move is invisible to callers.
"""

from __future__ import annotations

import pytest

import agentloop.store as store

PUBLIC = {
    "ALLOWED_FINDING_TRANSITIONS",
    "DEFAULT_PAGE_SIZE",
    "FINDING_STATUSES",
    "FindingNotFoundError",
    "FindingTransitionError",
    "InvalidCursorError",
    "LABEL_KEY_PATTERN",
    "MAX_LABEL_VALUE_LENGTH",
    "MAX_PAGE_SIZE",
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
}
# Private helpers that other AgentLoop modules or tests import from here.
PRIVATE = {"_build_optimization_queue", "_find_api_key_row", "_read_postgres_password"}

# Where each moved name is implemented now.
HOMES = {
    "store_types": [
        "ALLOWED_FINDING_TRANSITIONS",
        "DEFAULT_PAGE_SIZE",
        "FINDING_STATUSES",
        "FindingNotFoundError",
        "FindingTransitionError",
        "MAX_PAGE_SIZE",
        "TraceProjectConflictError",
        "TraceStore",
    ],
    "store_common": [
        "LABEL_KEY_PATTERN",
        "MAX_LABEL_VALUE_LENGTH",
        "_build_optimization_queue",
        "_find_api_key_row",
        "parse_label_filter",
        "validate_label_filter",
    ],
    "store_sqlite": ["SQLiteTraceStore"],
    "store_postgres": ["PostgresTraceStore", "_read_postgres_password"],
    "store_keys": ["hash_api_key", "new_api_key", "verify_api_key_hash"],
}


def test_public_and_private_names_stay_importable_from_store():
    missing = sorted(name for name in PUBLIC | PRIVATE if not hasattr(store, name))
    assert missing == []
    assert set(store.__all__) == PUBLIC


@pytest.mark.parametrize("module", sorted(HOMES))
def test_moved_names_are_the_same_objects(module):
    import importlib

    home = importlib.import_module(f"agentloop.{module}")
    for name in HOMES[module]:
        assert getattr(store, name) is getattr(home, name), name


def test_api_key_helpers_keep_their_behaviour():
    key = store.new_api_key()
    assert key.startswith("al_")
    encoded = store.hash_api_key(key)
    assert encoded.startswith("scrypt$v1$")
    assert store.verify_api_key_hash(key, encoded) is True
    assert store.verify_api_key_hash(key + "x", encoded) is False
    assert store.verify_api_key_hash("no-prefix", encoded) is False


def test_get_store_picks_the_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "a.db"))
    assert isinstance(store.get_store(), store.SQLiteTraceStore)
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "postgres")
    for name in ("AGENTLOOP_DATABASE_URL", "DATABASE_URL", "PGHOST", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="requires"):
        store.get_store()


def test_key_lookup_uses_the_shared_verification(monkeypatch):
    # Both stores verify keys through store_common; patch it there.
    from agentloop import store_common

    calls = []

    def counting(api_key, encoded):
        calls.append(encoded)
        return False

    monkeypatch.setattr(store_common, "verify_api_key_hash", counting)
    assert store._find_api_key_row("al_x", ["row"], lambda row: "hash-" + row) is None
    assert calls == ["hash-row"]
