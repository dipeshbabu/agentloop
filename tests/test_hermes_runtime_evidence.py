from __future__ import annotations

import hashlib
import json

import pytest

from agentloop.tracer import AgentTrace
from scripts.smoke_hermes_runtime import HERMES_REVISIONS, address_allowed
from scripts.validate_hermes_runtime import REVISION, REVISIONS, artifact_bytes, qualified_trace


@pytest.fixture
def evidence(tmp_path):
    raw = AgentTrace("hermes.turn", metadata={"integration": "hermes"}).to_dict()
    raw["ended_at"] = raw["started_at"]
    raw["elapsed_ms"] = 0
    native = json.loads(json.dumps(raw))
    native["metadata"].update(
        synthetic=True,
        source="hermes_runtime_pilot",
        pilot={"scenario": "stream", "synthetic_provider": True, "hermes_revision": REVISION},
    )
    artifact = {}
    for name, payload in (("raw", raw), ("native", native)):
        content = json.dumps(payload).encode()
        (tmp_path / (name + ".json")).write_bytes(content)
        artifact[name] = name + ".json"
        artifact[name + "_sha256"] = hashlib.sha256(content).hexdigest()
    return tmp_path, artifact


def test_runtime_qualification_preserves_evidence(evidence):
    root, artifact = evidence
    trace = qualified_trace(root, artifact, "stream")
    assert trace.metadata["synthetic"] is True


def test_runtime_pins_match_the_validator():
    assert HERMES_REVISIONS == REVISIONS


@pytest.mark.parametrize("revision", REVISIONS)
def test_runtime_qualification_accepts_only_the_declared_pin(evidence, revision):
    root, artifact = evidence
    native = json.loads((root / "native.json").read_bytes())
    native["metadata"]["pilot"]["hermes_revision"] = revision
    content = json.dumps(native).encode()
    (root / "native.json").write_bytes(content)
    artifact["native_sha256"] = hashlib.sha256(content).hexdigest()
    trace = qualified_trace(root, artifact, "stream", revision=revision)
    assert trace.metadata["pilot"]["hermes_revision"] == revision
    other = next(value for value in REVISIONS if value != revision)
    with pytest.raises(ValueError, match="changed captured evidence"):
        qualified_trace(root, artifact, "stream", revision=other)
    with pytest.raises(ValueError, match="Wrong Hermes source revision"):
        qualified_trace(root, artifact, "stream", revision="unknown")


def test_runtime_evidence_rejects_changed_bytes(evidence):
    root, artifact = evidence
    (root / "native.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        qualified_trace(root, artifact, "stream")


def test_runtime_evidence_rejects_rehashed_measurement_changes(evidence):
    root, artifact = evidence
    payload = json.loads((root / "native.json").read_bytes())
    payload["elapsed_ms"] = 123
    content = json.dumps(payload).encode()
    (root / "native.json").write_bytes(content)
    artifact["native_sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError, match="changed captured evidence"):
        qualified_trace(root, artifact, "stream")


@pytest.mark.parametrize(
    "path", ["../outside.json", "/outside.json", "C:/outside.json", "..\\outside.json"]
)
def test_runtime_receipts_cannot_read_outside_the_scenario(tmp_path, path):
    with pytest.raises(ValueError, match="Invalid artifact path"):
        artifact_bytes(tmp_path, path, "0" * 64)


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        (("127.0.0.1", 1234), True),
        (("::1", 1234), True),
        (("127.0.0.1", 443), False),
        (("203.0.113.10", 1234), False),
        (("provider.example", 1234), False),
        ("/tmp/other.sock", False),
    ],
)
def test_runtime_network_guard_limits_clients_to_the_fixture(address, allowed):
    assert address_allowed(address, 1234) is allowed
