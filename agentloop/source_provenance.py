"""Opt-in trace-to-source provenance for traced functions (issue #395).

A traced function can carry a small *source receipt* in its span metadata under
``agentloop.source``. The receipt identifies the code without containing it:

```json
{
  "schema": "agentloop.source/v1",
  "status": "captured",
  "path": "agents/research.py",
  "qualname": "Researcher.search",
  "module": "agents.research",
  "line_start": 41,
  "line_end": 58,
  "content_sha256": "…",
  "revision": "optional, informational"
}
```

``path`` is relative to a source root and always uses ``/``. Absolute paths and
source text are never recorded. A function whose file lies outside the root, or
whose file cannot be read, still gets a receipt with its qualified name and an
explicit ``status`` (``outside_root`` or ``unavailable``) but no path.

``content_sha256`` hashes the whole file as UTF-8 text with newlines normalized
to ``\\n``, so a checkout made with different line-ending settings still matches.
``line_start`` includes decorators (Python's ``co_firstlineno``).

:func:`resolve_source` checks a receipt against a checkout. Only an exact match
(same relative path, same file digest, exactly one definition of the qualified
name at the recorded lines) is ``resolved`` and eligible for rewrite planning.
Every other outcome stays explicit: ``moved``, ``stale``, ``ambiguous``,
``file_missing``, ``symbol_missing``, ``unverified``, ``invalid``,
``outside_root`` or ``unavailable``.

Receipts from other tools that follow the OpenTelemetry code attributes
(``code.file.path`` / ``code.filepath``, ``code.function.name`` /
``code.function``, ``code.line.number`` / ``code.lineno``) are read as
``unverified`` evidence: they have no digest, so they never become eligible.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

SOURCE_KEY = "agentloop.source"
SOURCE_SCHEMA = "agentloop.source/v1"
CAPTURE_ENV = "AGENTLOOP_CAPTURE_SOURCE"
ROOT_ENV = "AGENTLOOP_SOURCE_ROOT"
REVISION_ENV = "AGENTLOOP_SOURCE_REVISION"

#: Statuses a receipt can carry when it is captured.
CAPTURE_STATUSES = frozenset({"captured", "outside_root", "unavailable"})
#: Resolution statuses. Only ``resolved`` is eligible for rewrite planning.
RESOLUTION_STATUSES = (
    "resolved",
    "moved",
    "stale",
    "ambiguous",
    "file_missing",
    "symbol_missing",
    "unverified",
    "invalid",
    "outside_root",
    "unavailable",
)

_TRUE = {"1", "true", "yes", "on"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_QUALNAME = re.compile(r"^[A-Za-z_][\w]*(\.(<locals>|[A-Za-z_][\w]*))*$")
_MAX_FIELD = 512


def capture_enabled(explicit: bool | None = None) -> bool:
    """Whether to capture source receipts: an explicit choice, else the env flag."""
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(CAPTURE_ENV, "").strip().lower() in _TRUE


def text_digest(text: str) -> str:
    """SHA-256 of source text with newlines normalized to ``\\n``."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1024)
def _file_digest(path: str, mtime_ns: int, size: int) -> str | None:
    # mtime and size are part of the cache key so an edited file is re-read.
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return text_digest(handle.read())
    except (OSError, UnicodeDecodeError):
        return None


def _digest_of(path: str) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _file_digest(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _repository_root(directory: str) -> str | None:
    current = directory
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _source_root(file_path: str, root: str | os.PathLike[str] | None) -> str:
    if root is not None:
        return os.path.realpath(os.fspath(root))
    configured = os.environ.get(ROOT_ENV)
    if configured:
        return os.path.realpath(configured)
    return _repository_root(os.path.dirname(file_path)) or os.path.realpath(os.getcwd())


def _relative(file_path: str, root: str) -> str | None:
    """A ``/``-separated path under ``root``, or None when the file is outside it."""
    try:
        relative = os.path.relpath(file_path, root)
    except ValueError:  # different drives on Windows
        return None
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return None
    return Path(relative).as_posix()


def capture_source(
    func: Callable[..., Any],
    *,
    root: str | os.PathLike[str] | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Build a source receipt for ``func`` without recording its source or absolute path.

    ``root`` defaults to ``AGENTLOOP_SOURCE_ROOT``, else the nearest enclosing git
    checkout, else the current directory. ``revision`` defaults to
    ``AGENTLOOP_SOURCE_REVISION`` and is informational; validation uses the digest.
    Never raises: anything that cannot be determined is reported in ``status``.
    """
    target = inspect.unwrap(func) if callable(func) else func
    qualname = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    receipt: dict[str, Any] = {"schema": SOURCE_SCHEMA, "status": "unavailable"}
    if isinstance(qualname, str):
        receipt["qualname"] = qualname
    module = getattr(target, "__module__", None)
    if isinstance(module, str):
        receipt["module"] = module
    chosen_revision = revision if revision is not None else os.environ.get(REVISION_ENV)
    if chosen_revision:
        receipt["revision"] = str(chosen_revision)[:_MAX_FIELD]
    try:
        source_file = inspect.getsourcefile(target)
        lines, line_start = inspect.getsourcelines(target)
    except (OSError, TypeError):
        return receipt
    if not source_file or not lines:
        return receipt
    real_file = os.path.realpath(source_file)
    if not os.path.isfile(real_file):
        # Code from exec(), python -c or a notebook cell has no file to point at.
        return receipt
    relative = _relative(real_file, _source_root(real_file, root))
    if relative is None:
        receipt["status"] = "outside_root"
        return receipt
    digest = _digest_of(real_file)
    receipt.update(
        {
            "status": "captured",
            "path": relative,
            "line_start": int(line_start),
            "line_end": int(line_start) + len(lines) - 1,
        }
    )
    if digest is not None:
        receipt["content_sha256"] = digest
    return receipt


def receipt_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The span's source receipt, or an ``unverified`` one built from OTel code attributes."""
    if not isinstance(metadata, Mapping):
        return None
    native = metadata.get(SOURCE_KEY)
    if isinstance(native, Mapping):
        return dict(native)
    path = _first(metadata, "code.file.path", "code.filepath")
    function = _first(metadata, "code.function.name", "code.function")
    if not isinstance(path, str) and not isinstance(function, str):
        return None
    namespace = metadata.get("code.namespace")
    qualname = function
    if isinstance(function, str) and isinstance(namespace, str) and "." not in function:
        qualname = f"{namespace}.{function}"
    receipt: dict[str, Any] = {"schema": "otel.code", "status": "captured"}
    if isinstance(path, str):
        receipt["path"] = path
    if isinstance(qualname, str):
        receipt["qualname"] = qualname
    line = _first(metadata, "code.line.number", "code.lineno")
    if isinstance(line, int) and not isinstance(line, bool):
        receipt["line_start"] = line
    return receipt


def _first(metadata: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metadata:
            return metadata[key]
    return None


def _safe_relative_path(value: Any) -> str | None:
    """Accept only a normalized relative path that cannot leave the checkout."""
    if not isinstance(value, str) or not value or len(value) > _MAX_FIELD:
        return None
    if "\x00" in value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    normalized = posixpath.normpath(value)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized != value:
        return None
    return normalized


def _valid_qualname(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= _MAX_FIELD and _QUALNAME.match(value):
        return value
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def python_definitions(text: str) -> dict[str, list[tuple[int, int]]]:
    """Map each qualified name in ``text`` to its decorator-inclusive line ranges.

    Nested functions use ``<locals>`` like ``__qualname__``. A name defined more
    than once in the same scope keeps every definition, so duplicates stay visible.
    Returns an empty mapping when the text does not parse.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, list[tuple[int, int]]] = {}

    def visit(body: Iterable[ast.AST], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                qualname = f"{prefix}{node.name}"
                start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
                end = getattr(node, "end_lineno", None) or node.lineno
                found.setdefault(qualname, []).append((start, end))
                inner = (
                    f"{qualname}.<locals>."
                    if not isinstance(node, ast.ClassDef)
                    else f"{qualname}."
                )
                visit(node.body, inner)
            else:
                # Definitions inside if/try/with/for blocks share the enclosing scope.
                for field in ("body", "orelse", "finalbody", "handlers"):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        visit(children, prefix)
                for case in getattr(node, "cases", []) or []:
                    visit(case.body, prefix)

    visit(tree.body, "")
    return found


class SourceIndex:
    """Read-only view of a checkout's source files used to resolve receipts.

    ``files`` maps a ``/``-separated relative path to its text, as read with
    universal newlines. Digests and parsed definitions are computed lazily, once.
    """

    def __init__(self, files: Mapping[str, str]) -> None:
        self._files = dict(files)
        self._digests: dict[str, str] = {}
        self._definitions: dict[str, dict[str, list[tuple[int, int]]]] = {}
        self._by_digest: dict[str, list[str]] | None = None

    def has(self, path: str) -> bool:
        return path in self._files

    def digest(self, path: str) -> str:
        if path not in self._digests:
            self._digests[path] = text_digest(self._files[path])
        return self._digests[path]

    def definitions(self, path: str) -> dict[str, list[tuple[int, int]]]:
        if path not in self._definitions:
            text = self._files[path]
            self._definitions[path] = python_definitions(text) if path.endswith(".py") else {}
        return self._definitions[path]

    def paths_with_digest(self, digest: str) -> list[str]:
        if self._by_digest is None:
            self._by_digest = {}
            for path in sorted(self._files):
                self._by_digest.setdefault(self.digest(path), []).append(path)
        return self._by_digest.get(digest, [])


def resolve_source(receipt: Mapping[str, Any] | None, index: SourceIndex) -> dict[str, Any]:
    """Resolve one receipt against a checkout; never raises on malformed input."""
    if not isinstance(receipt, Mapping):
        return {"status": "unavailable", "reason": "span has no source receipt"}
    status = receipt.get("status", "captured")
    qualname = _valid_qualname(receipt.get("qualname"))
    base: dict[str, Any] = {"qualname": qualname} if qualname else {}
    if status in {"outside_root", "unavailable"}:
        reason = (
            "the traced function's file is outside the recorded source root"
            if status == "outside_root"
            else "the traced function's source could not be located when it was recorded"
        )
        return {**base, "status": status, "reason": reason}
    path = _safe_relative_path(receipt.get("path"))
    if path is None or qualname is None or status != "captured":
        return {
            **base,
            "status": "invalid",
            "reason": "the receipt's path or qualified name is missing, absolute or malformed",
        }
    digest = receipt.get("content_sha256")
    digest = digest if isinstance(digest, str) and _DIGEST.match(digest) else None
    line_start = _positive_int(receipt.get("line_start"))
    line_end = _positive_int(receipt.get("line_end"))
    base["recorded_path"] = path

    if digest is None:
        # Without a digest the checkout cannot be shown to be the recorded one.
        located = _locate(index, path, qualname, line_start, None) if index.has(path) else None
        return {
            **base,
            **(located or {}),
            "status": "unverified",
            "reason": "the receipt has no content digest, so the checkout cannot be verified",
        }

    if index.has(path) and index.digest(path) == digest:
        located = _locate(index, path, qualname, line_start, line_end)
        if located is None:
            return {
                **base,
                "status": "symbol_missing",
                "path": path,
                "reason": "the file matches but defines no such qualified name at the recorded lines",
            }
        if located.get("ambiguous"):
            return {
                **base,
                **located,
                "status": "ambiguous",
                "reason": "the qualified name is defined more than once at matching lines",
            }
        return {
            **base,
            **located,
            "status": "resolved",
            "reason": "path, content digest and definition match",
        }

    copies = [item for item in index.paths_with_digest(digest) if item != path]
    if len(copies) > 1:
        return {
            **base,
            "status": "ambiguous",
            "candidates": copies[:8],
            "reason": "identical copies of the recorded file exist at several paths",
        }
    if len(copies) == 1:
        located = _locate(index, copies[0], qualname, line_start, line_end)
        if located is None or located.get("ambiguous"):
            return {
                **base,
                "status": "symbol_missing" if located is None else "ambiguous",
                "path": copies[0],
                "reason": "the recorded file was moved and the definition could not be matched",
            }
        return {
            **base,
            **located,
            "status": "moved",
            "reason": f"the recorded file now lives at {copies[0]}",
        }

    if index.has(path):
        located = _locate(index, path, qualname, None, None)
        return {
            **base,
            **(located or {"path": path}),
            "status": "stale",
            "reason": "the file changed since the trace was recorded",
        }
    return {**base, "status": "file_missing", "reason": "the recorded file is not in this checkout"}


def _locate(
    index: SourceIndex,
    path: str,
    qualname: str,
    line_start: int | None,
    line_end: int | None,
) -> dict[str, Any] | None:
    ranges = index.definitions(path).get(qualname, [])
    if line_start is not None:
        ranges = [item for item in ranges if item[0] == line_start] or (
            [] if line_end is not None else ranges
        )
    if line_end is not None:
        ranges = [item for item in ranges if item[1] == line_end]
    if not ranges:
        return None
    location: dict[str, Any] = {"path": path, "line_start": ranges[0][0], "line_end": ranges[0][1]}
    if len(ranges) > 1:
        location["ambiguous"] = True
        location["candidate_lines"] = [list(item) for item in ranges[:8]]
    return location


__all__ = [
    "CAPTURE_ENV",
    "REVISION_ENV",
    "ROOT_ENV",
    "SOURCE_KEY",
    "SOURCE_SCHEMA",
    "SourceIndex",
    "capture_enabled",
    "capture_source",
    "python_definitions",
    "receipt_from_metadata",
    "resolve_source",
    "text_digest",
]
