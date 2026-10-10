"""Bounded JSON and local artifact references for read-only import adapters."""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any


class ImportValidationError(ValueError):
    """A payload-free diagnostic: never include untrusted values in messages."""

    def __init__(self, code: str, field: str, reason: str) -> None:
        self.code, self.field, self.reason = code, field, reason
        super().__init__(f"{field}: {reason}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "reason": self.reason}


@dataclass(frozen=True)
class ImportLimits:
    max_json_bytes: int = 8 * 1024 * 1024
    max_line_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_records: int = 10_000
    max_depth: int = 64
    max_nodes: int = 200_000
    max_trials: int = 10_000
    max_trajectories: int = 256
    max_spans: int = 100_000
    max_events_per_trace: int = 10_000
    max_references: int = 1_024
    max_path_depth: int = 32
    max_metadata_bytes: int = 64 * 1024
    max_number_chars: int = 128

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ImportValidationError(
                    "invalid_limit", item.name, "must be a positive integer"
                )
        # The JSON decoder is recursive. Raising this setting must not bypass
        # the preflight protection or expose platform-dependent recursion limits.
        if self.max_depth > 128:
            raise ImportValidationError("invalid_limit", "max_depth", "must not exceed 128")


@dataclass
class ImportBudget:
    """Shared counters for adapters; rejected records still consume read bytes."""

    limits: ImportLimits
    total_bytes: int = 0
    records: int = 0
    trials: int = 0
    trajectories: int = 0
    spans: int = 0
    references: int = 0

    def consume(self, **counts: int) -> None:
        pending = {}
        for key, count in counts.items():
            if key not in {
                "total_bytes",
                "records",
                "trials",
                "trajectories",
                "spans",
                "references",
            }:
                raise ImportValidationError("invalid_counter", "budget", "unknown counter")
            if type(count) is not int or count < 0:
                raise ImportValidationError(
                    "invalid_counter", "budget", "count must be nonnegative"
                )
            value = getattr(self, key) + count
            if value > getattr(self.limits, "max_" + key):
                raise ImportValidationError("limit_exceeded", key, "import budget exceeded")
            pending[key] = value
        for key, value in pending.items():
            setattr(self, key, value)


def _reject_constant(_: str) -> Any:
    raise ImportValidationError("invalid_number", "json", "non-finite numbers are unsupported")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportValidationError(
                "duplicate_key", "json", "duplicate object keys are ambiguous"
            )
        result[key] = value
    return result


def _preflight_depth(data: bytes, limit: int) -> None:
    depth = 0
    quoted = escaped = False
    for char in data:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > limit:
                raise ImportValidationError(
                    "limit_exceeded", "json.depth", "nesting limit exceeded"
                )
        elif char in (93, 125):
            depth -= 1


def validate_json_tree(value: Any, limits: ImportLimits = ImportLimits()) -> None:
    """Check Python inputs too; no implicit tuple/key coercion or non-finite JSON."""

    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > limits.max_nodes:
            raise ImportValidationError("limit_exceeded", "json.nodes", "node limit exceeded")
        if type(node) in {dict, list}:
            if depth + 1 > limits.max_depth:
                raise ImportValidationError(
                    "limit_exceeded", "json.depth", "nesting limit exceeded"
                )
            if len(node) + len(pending) + count > limits.max_nodes:
                raise ImportValidationError("limit_exceeded", "json.nodes", "node limit exceeded")
            if isinstance(node, dict):
                if any(type(key) is not str for key in node):
                    raise ImportValidationError(
                        "invalid_json", "json", "object keys must be strings"
                    )
                for key in node:
                    try:
                        key.encode("utf-8")
                    except UnicodeError as exc:
                        raise ImportValidationError(
                            "invalid_json", "json", "strings must be valid UTF-8"
                        ) from exc
                pending.extend((item, depth + 1) for item in node.values())
            else:
                pending.extend((item, depth + 1) for item in node)
        elif type(node) is float:
            if not math.isfinite(node):
                raise ImportValidationError("invalid_number", "json", "numbers must be finite")
        elif type(node) is int:
            if (
                node.bit_length() > limits.max_number_chars * 4
                or len(str(node)) > limits.max_number_chars
            ):
                raise ImportValidationError(
                    "limit_exceeded", "json.number", "number length exceeded"
                )
        elif type(node) is str:
            try:
                node.encode("utf-8")
            except UnicodeError as exc:
                raise ImportValidationError(
                    "invalid_json", "json", "strings must be valid UTF-8"
                ) from exc
        elif node is not None and type(node) not in {str, bool}:
            raise ImportValidationError("invalid_json", "json", "unsupported JSON value")


def parse_json_bytes(data: bytes, limits: ImportLimits = ImportLimits()) -> Any:
    if len(data) > limits.max_json_bytes:
        raise ImportValidationError("limit_exceeded", "json.bytes", "JSON byte limit exceeded")
    _preflight_depth(data, limits.max_depth)

    def number(text: str, convert: Any) -> Any:
        if len(text) > limits.max_number_chars:
            raise ImportValidationError("limit_exceeded", "json.number", "number length exceeded")
        return convert(text)

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=lambda text: number(text, int),
            parse_float=lambda text: number(text, float),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ImportValidationError("invalid_json", "json", "invalid UTF-8 JSON document") from exc
    validate_json_tree(value, limits)
    return value


_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def relative_reference(value: Any, limits: ImportLimits = ImportLimits()) -> str:
    """Require normalized POSIX relative names portable to Windows and Unix."""

    try:
        length = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeError as exc:
        raise ImportValidationError("unsafe_path", "reference", "path must be valid UTF-8") from exc
    if not isinstance(value, str) or not value or length > 4096:
        raise ImportValidationError("unsafe_path", "reference", "must be a bounded relative path")
    if (
        "\\" in value
        or ":" in value
        or any(ord(char) < 32 or char in '<>"|?*' for char in value)
        or PureWindowsPath(value).drive
        or value.startswith("/")
    ):
        raise ImportValidationError(
            "unsafe_path", "reference", "absolute, remote or ambiguous path"
        )
    parts = value.split("/")
    if len(parts) > limits.max_path_depth:
        raise ImportValidationError("limit_exceeded", "reference.depth", "path depth exceeded")
    if any(
        part in {"", ".", ".."} or part.endswith((" ", ".")) or _DEVICE.match(part)
        for part in parts
    ):
        raise ImportValidationError(
            "unsafe_path", "reference", "path is not normalized or portable"
        )
    return value


def safe_artifact_path(
    root: str | Path, reference: str, limits: ImportLimits = ImportLimits()
) -> Path:
    reference = relative_reference(reference, limits)
    try:
        base = Path(root).resolve(strict=True)
        if not base.is_dir():
            raise ImportValidationError("invalid_root", "root", "artifact root must be a directory")
        candidate = base
        for part in reference.split("/"):
            candidate = candidate / part
            # Path.is_junction() is absent on Python 3.10/3.11. Windows lstat
            # exposes reparse attributes on those interpreters too, so do not
            # accidentally allow in-root junctions on our oldest supported API.
            reparse = getattr(candidate.lstat(), "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            if (
                candidate.is_symlink()
                or reparse
                or getattr(candidate, "is_junction", lambda: False)()
            ):
                raise ImportValidationError(
                    "unsafe_path", "reference", "symlinks and junctions are unsupported"
                )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(base):
            raise ImportValidationError("unsafe_path", "reference", "path leaves artifact root")
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ImportValidationError(
                "invalid_artifact", "reference", "artifact must be a regular file"
            )
        return resolved
    except OSError as exc:
        raise ImportValidationError(
            "missing_artifact", "reference", "artifact is unavailable"
        ) from exc
    except RuntimeError as exc:
        raise ImportValidationError(
            "unsafe_path", "reference", "unresolvable artifact path"
        ) from exc


@dataclass(frozen=True)
class JsonArtifact:
    reference: str
    artifact_sha256: str
    byte_count: int
    payload: Any


def load_json_artifact(
    root: str | Path,
    reference: str,
    *,
    limits: ImportLimits = ImportLimits(),
    budget: ImportBudget | None = None,
) -> JsonArtifact:
    if budget is not None and budget.limits != limits:
        raise ImportValidationError(
            "invalid_limit", "budget", "budget and parser limits must match"
        )
    path = safe_artifact_path(root, reference, limits)
    bound = limits.max_json_bytes
    if budget is not None:
        if budget.records >= limits.max_records:
            raise ImportValidationError("limit_exceeded", "records", "import budget exceeded")
        bound = min(bound, limits.max_total_bytes - budget.total_bytes)
    try:
        with path.open("rb") as stream:
            data = stream.read(bound + 1)
        # Recheck static references before accepting data. This is not an OS
        # sandbox or an atomic defense against concurrent filesystem mutation.
        if safe_artifact_path(root, reference, limits) != path:
            raise ImportValidationError(
                "unsafe_path", "reference", "artifact path changed while reading"
            )
    except OSError as exc:
        raise ImportValidationError(
            "invalid_artifact", "reference", "artifact could not be read"
        ) from exc
    if budget is not None:
        budget.consume(total_bytes=len(data), records=1)
    payload = parse_json_bytes(data, limits)
    return JsonArtifact(reference, sha256(data).hexdigest(), len(data), payload)
