"""Owned local artifacts; exclusive journal lock and atomic immutable writes."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from agentloop.experiment_types import canonical


def local_path(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValueError("experiment artifact path escapes its root")
    return target


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_once(root, relative, value):
    return write_bytes_once(root, relative, (canonical(value) + "\n").encode("utf-8"))


def write_bytes_once(root, relative, encoded):
    path = local_path(root, relative)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("existing experiment artifact has conflicting content")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path(
        root, str(path.relative_to(Path(root).resolve())) + "." + uuid4().hex + ".tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # All journal writers hold the same exclusive lock. Existing artifacts
        # are compared above; an interrupted temporary is never a completed record.
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


@contextmanager
def journal_lock(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = local_path(root, "journal.lock")
    nonce = uuid4().hex
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(nonce)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValueError(
            "experiment journal is busy or requires host lock reconciliation"
        ) from None
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if path.read_text(encoding="utf-8") == nonce:
                path.unlink()
        except BaseException:
            if not failed:
                raise
