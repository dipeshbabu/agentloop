"""Trusted host-side recovery journal contract and a bounded, non-durable example."""

from __future__ import annotations

import json
from threading import RLock
from typing import Protocol
from uuid import uuid4

from agentloop.checkpoint_state import CheckpointError, _require, _text
from agentloop.context_types import canonical, fingerprint


class RecoveryStore(Protocol):
    """The host must authenticate owners and atomically claim the latest record.

    Production implementations must persist claims and records together. A claim
    is never automatically released: an interrupted/uncertain resume needs host
    reconciliation. Graph state remains in the framework's own checkpointer.
    """

    atomic_claim: bool
    durable: bool
    schema_version: str

    def save(self, owner, stream, record, *, previous_ref=None, request_id=None) -> str: ...

    def load(self, owner, stream, reference) -> dict: ...

    def claim(self, owner, stream, reference, request_id, record_hash) -> bool: ...


class MemoryRecoveryStore:
    """Thread-safe bounded fake journal; loses records and claims on restart."""

    atomic_claim = True
    durable = False
    schema_version = "1.0"

    def __init__(self, *, max_records=256):
        _require(type(max_records) is int and 1 <= max_records <= 10000, "invalid_store_limit")
        self._maximum = max_records
        self._lock = RLock()
        self._records, self._latest, self._claims, self._requests = {}, {}, {}, set()

    def save(self, owner, stream, record, *, previous_ref=None, request_id=None):
        _require(_text(owner) and _text(stream), "invalid_owner")
        encoded = canonical(record)
        _require(len(encoded.encode("utf-8")) <= 4_100_000, "state_size_limit")
        key = (owner, stream)
        with self._lock:
            _require(len(self._records) < self._maximum, "store_limit")
            _require(self._latest.get(key) == previous_ref, "stale_checkpoint")
            if previous_ref is not None:
                _require(
                    _text(request_id) and self._claims.get(previous_ref) == request_id,
                    "claim_required",
                )
            reference = "checkpoint_" + uuid4().hex
            self._records[reference] = (key, encoded)
            self._latest[key] = reference
            return reference

    def load(self, owner, stream, reference):
        with self._lock:
            entry = self._records.get(reference)
            if entry is None:
                raise CheckpointError("missing_checkpoint")
            _require(entry[0] == (owner, stream), "checkpoint_owner_mismatch")
            return json.loads(entry[1])

    def claim(self, owner, stream, reference, request_id, record_hash):
        _require(_text(request_id), "invalid_request_id")
        key = (owner, stream)
        with self._lock:
            record = self.load(owner, stream, reference)
            _require(fingerprint(record) == record_hash, "checkpoint_changed")
            if (
                self._latest.get(key) != reference
                or reference in self._claims
                or (key, request_id) in self._requests
            ):
                return False
            self._claims[reference] = request_id
            self._requests.add((key, request_id))
            return True
