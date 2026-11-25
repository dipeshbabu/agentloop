"""Contract for agentloop.store's public names while it is split into modules (#407).

Code outside AgentLoop imports these names from ``agentloop.store``. Moving an
implementation to another module must keep every one importable from here,
as the same object, so the move is invisible to callers.
"""

from __future__ import annotations

import agentloop.store as store

PUBLIC = {
    "DEFAULT_PAGE_SIZE",
    "FINDING_STATUSES",
    "FindingNotFoundError",
    "FindingTransitionError",
    "InvalidCursorError",
    "MAX_PAGE_SIZE",
    "PostgresTraceStore",
    "SQLiteTraceStore",
    "TraceProjectConflictError",
    "TraceStore",
    "get_store",
    "hash_api_key",
    "new_api_key",
    "parse_label_filter",
    "validate_label_filter",
    "verify_api_key_hash",
}


def test_public_names_stay_importable_from_store():
    missing = sorted(name for name in PUBLIC if not hasattr(store, name))
    assert missing == []


def test_api_key_helpers_keep_their_behaviour():
    key = store.new_api_key()
    assert key.startswith("al_")
    encoded = store.hash_api_key(key)
    assert encoded.startswith("scrypt$v1$")
    assert store.verify_api_key_hash(key, encoded) is True
    assert store.verify_api_key_hash(key + "x", encoded) is False
    assert store.verify_api_key_hash("no-prefix", encoded) is False


def test_moved_helpers_are_the_same_objects():
    from agentloop import store_keys

    for name in ("hash_api_key", "verify_api_key_hash", "new_api_key"):
        assert getattr(store, name) is getattr(store_keys, name)


def test_monkeypatching_verification_through_store_still_works(monkeypatch):
    # Store methods look verify_api_key_hash up in agentloop.store, so existing
    # tests and callers that patch it there keep working after the move.
    calls = []

    def counting(api_key, encoded):
        calls.append(encoded)
        return False

    monkeypatch.setattr(store, "verify_api_key_hash", counting)
    assert store._find_api_key_row("al_x", ["row"], lambda row: "hash-" + row) is None
    assert calls == ["hash-row"]
