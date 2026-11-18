"""Explicit local verifier snapshots for guided comparisons."""

from __future__ import annotations

import hashlib
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from agentloop.quality import _MISSING, _trace_output

DEFAULT_TIMEOUT = 30.0
_MAX_SOURCE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LocalVerifier:
    source: bytes = field(repr=False)
    sha256: str
    timeout_s: float

    @classmethod
    def read(cls, path, timeout_s):
        if (
            type(timeout_s) not in (int, float)
            or not 0 < timeout_s <= 3600
            or not math.isfinite(timeout_s)
        ):
            raise ValueError("Verifier timeout must be positive and at most 3600 seconds")
        path = Path(path)
        if not path.is_file() or path.suffix.lower() != ".py":
            raise ValueError("Select an existing Python verifier file with --verifier")
        with path.open("rb") as handle:
            source = handle.read(_MAX_SOURCE_BYTES + 1)
        if not source or len(source) > _MAX_SOURCE_BYTES:
            raise ValueError("Verifier source must be between 1 byte and 1 MiB")
        return cls(source, hashlib.sha256(source).hexdigest(), float(timeout_s))

    def declaration(self):
        return {"sha256": self.sha256, "timeout_s": self.timeout_s}

    def check(self, task, repetition, trace, *, usable):
        if not usable:
            return {
                "status": "unavailable",
                "passed": False,
                "detail": "Attempt has no usable trace",
            }
        output = _trace_output(trace)
        if output is _MISSING:
            return {
                "status": "unavailable",
                "passed": False,
                "detail": "Trace has no output for verification",
            }
        from agentloop.improvement import _invoke

        request = {
            "schema_version": "1.0",
            "task_id": task["id"],
            "repetition": repetition,
            "input": task["input"],
            "output": output,
        }
        with TemporaryDirectory(prefix="agentloop-verifier-") as directory:
            # Execute the exact checked bytes even if the selected file changes.
            snapshot = Path(directory) / "verifier.py"
            snapshot.write_bytes(self.source)
            result = _invoke([sys.executable, "-I", str(snapshot)], request, self.timeout_s)
        state = result["status"]
        detail = {
            "completed": "Verifier exited successfully",
            "failed": f"Verifier exited with code {result['exit_code']}",
            "timed_out": f"Verifier exceeded its {self.timeout_s:g}-second timeout",
            "cancelled": "Verifier was interrupted",
            "runner_error": "Could not start the verifier",
        }[state]
        return {"status": state, "passed": state == "completed", "detail": detail}


def resume_verifier(declaration, path):
    if declaration is None and path is None:
        return None
    if path is None:
        raise ValueError("This session requires an explicit --verifier matching the frozen source")
    if not isinstance(declaration, dict) or set(declaration) != {"sha256", "timeout_s"}:
        raise ValueError("The session must freeze the local verifier in improve start")
    verifier = LocalVerifier.read(path, declaration["timeout_s"])
    if verifier.declaration() != declaration:
        raise ValueError(
            "Verifier source changed since improve start. Freeze a new session to change it"
        )
    return verifier
