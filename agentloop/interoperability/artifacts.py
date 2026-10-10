"""Stable local native projections and receipts shared by external importers."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from agentloop.interoperability.contracts import ImportReceipt
from agentloop.interoperability.validation import (
    ImportValidationError,
    indirect_path,
    relative_reference,
)
from agentloop.interventions import canonical_json
from agentloop.tracer import AgentTrace


def native_bytes(trace: AgentTrace) -> bytes:
    return (
        json.dumps(trace.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_artifact(root: Path, reference: str, data: bytes) -> None:
    """Reject indirect paths and conflicting bytes under an explicit output root."""
    write_artifact_chunks(root, reference, (data,))


def write_artifact_chunks(root: Path, reference: str, chunks: Iterable[bytes]) -> None:
    """Write bounded chunks, comparing an existing artifact without loading it all."""
    relative_reference(reference)
    current = root
    for part in reference.split("/"):
        current = current / part
        try:
            indirect = indirect_path(current)
        except FileNotFoundError:
            indirect = False
        if indirect:
            raise ImportValidationError(
                "unsafe_path", "output", "output references cannot be symlinks"
            )
    target = root / reference
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with target.open("rb") as stream:
            for chunk in chunks:
                if stream.read(len(chunk)) != chunk:
                    raise ImportValidationError(
                        "output_conflict", "output", "existing artifact has conflicting content"
                    )
            if stream.read(1):
                raise ImportValidationError(
                    "output_conflict", "output", "existing artifact has conflicting content"
                )
        return
    with target.open("xb") as stream:
        for chunk in chunks:
            stream.write(chunk)


def write_import_bundle(
    traces: Iterable[AgentTrace],
    receipts: Iterable[ImportReceipt],
    out: str | Path,
    inventory: dict[str, Any],
) -> Path:
    """Verify captured byte hashes before exporting a reproducible import bundle."""
    traces, receipts = tuple(traces), tuple(receipts)
    expected = {
        item["run_id"]: item["trace_sha256"]
        for receipt in receipts
        for item in receipt.to_dict()["traces"]
    }
    if len({trace.run_id for trace in traces}) != len(traces):
        raise ImportValidationError(
            "source_conflict", "native_trace", "duplicate native trace identity"
        )
    for trace in traces:
        if sha256(native_bytes(trace)).hexdigest() != expected.get(trace.run_id):
            raise ImportValidationError(
                "source_conflict", "native_trace", "trace changed after receipt capture"
            )
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    for trace in traces:
        write_artifact(root, f"traces/{trace.run_id}.json", native_bytes(trace))
    for receipt in receipts:
        write_artifact(
            root,
            f"receipts/{receipt.receipt_id}.json",
            (canonical_json(receipt.to_dict()) + "\n").encode(),
        )
    write_artifact(root, "inventory.json", (canonical_json(inventory) + "\n").encode())
    return root / "inventory.json"
