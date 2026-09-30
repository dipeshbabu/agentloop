"""Bounded multipart transport for larger native research artifacts, with exact restore."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CHUNK_BYTES = 500_000
MAX_LOGICAL_BYTES = 512 * 1024 * 1024


def pack_evidence(root, out):
    from examples.real_agent_study.archive import pack

    root = Path(root).resolve()
    files, declarations = {}, []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("evidence must not contain symlinks")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_LOGICAL_BYTES:
            raise ValueError("logical evidence exceeds 512 MiB")
        relative = path.relative_to(root).as_posix()
        compressed = gzip.compress(data, compresslevel=9, mtime=0)
        identifier = hashlib.sha256(relative.encode()).hexdigest()
        names = []
        for index, offset in enumerate(range(0, len(compressed), CHUNK_BYTES)):
            name = f"chunks/{identifier}/{index:05d}.gzpart"
            files[name] = compressed[offset : offset + CHUNK_BYTES]
            names.append(name)
        declarations.append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "compressed_bytes": len(compressed),
                "chunks": names,
            }
        )
    manifest = {
        "schema_version": "1.0",
        "encoding": "gzip-split-v1",
        "max_logical_bytes": MAX_LOGICAL_BYTES,
        "total_logical_bytes": total,
        "files": declarations,
    }
    files["restore-manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    return pack(files, out)


def unpack_evidence(index, out):
    from examples.real_agent_study.archive import unpack

    out = Path(out).resolve()
    if out.exists():
        raise ValueError("restore destination must not exist")
    encoded = out.with_name(out.name + "-encoded")
    unpack(index, encoded)
    manifest = json.loads((encoded / "restore-manifest.json").read_text())
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("encoding") != "gzip-split-v1"
        or type(manifest.get("total_logical_bytes")) is not int
        or not 0 <= manifest["total_logical_bytes"] <= MAX_LOGICAL_BYTES
    ):
        raise ValueError("invalid evidence restore declaration")
    declarations = manifest["files"]
    if not isinstance(declarations, list) or len(declarations) > 100_000:
        raise ValueError("invalid restored file inventory")
    seen, total = set(), 0
    for entry in declarations:
        name = entry["path"]
        relative = PurePosixPath(name)
        if (
            not isinstance(name, str)
            or relative.is_absolute()
            or not relative.parts
            or any(part in {".", ".."} or ":" in part or "\\" in part for part in relative.parts)
        ):
            raise ValueError("unsafe restored path")
        target = (out / name).resolve()
        if not target.is_relative_to(out) or str(target).casefold() in seen:
            raise ValueError("duplicate or escaped restored path")
        seen.add(str(target).casefold())
        size = entry["bytes"]
        if (
            type(size) is not int
            or not 0 <= size <= MAX_LOGICAL_BYTES
            or not isinstance(entry["chunks"], list)
            or len(entry["chunks"]) > 1024
        ):
            raise ValueError("invalid restored file size/chunks")
        total += size
        if total > MAX_LOGICAL_BYTES:
            raise ValueError("restored evidence exceeds byte bound")
    if total != manifest["total_logical_bytes"]:
        raise ValueError("restored evidence size mismatch")
    out.mkdir(parents=True)
    for entry in declarations:
        chunks = []
        for name in entry["chunks"]:
            path = (encoded / name).resolve()
            if (
                not path.is_relative_to(encoded.resolve())
                or not name.startswith("chunks/")
                or path.stat().st_size > CHUNK_BYTES
            ):
                raise ValueError("unsafe compressed chunk")
            chunks.append(path.read_bytes())
        compressed = b"".join(chunks)
        if len(compressed) != entry["compressed_bytes"]:
            raise ValueError("compressed size mismatch")
        target = out / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with (
            gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source,
            target.open("xb") as destination,
        ):
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > entry["bytes"]:
                    raise ValueError("decompressed file exceeds declared size")
                digest.update(chunk)
                destination.write(chunk)
        if written != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise ValueError("restored file checksum mismatch")
    return len(declarations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pack", "unpack"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    value = (
        pack_evidence(args.source, args.out)
        if args.mode == "pack"
        else unpack_evidence(args.source, args.out)
    )
    print(json.dumps(value))
