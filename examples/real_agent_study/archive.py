"""Checksummed multipart evidence archives; no inference or executable loading."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_agent_study"

from .results import read_bundle


def pack(files, out):
    """Bound each checked-in part to 950 KB while retaining every entry."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("archive output must be fresh or empty")
    out.mkdir(parents=True, exist_ok=True)
    parts = []

    def encode(items):
        buffer = io.BytesIO()
        manifest = {
            "schema_version": "1.0",
            "files": {name: hashlib.sha256(value).hexdigest() for name, value in items},
        }
        with zipfile.ZipFile(
            buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, value in items:
                archive.writestr(name, value)
            archive.writestr("bundle-manifest.json", json.dumps(manifest, sort_keys=True))
        encoded = buffer.getvalue()
        if len(encoded) > 950000:
            if len(items) < 2:
                raise ValueError("one artifact exceeds the part size bound")
            middle = len(items) // 2
            encode(items[:middle])
            encode(items[middle:])
            return
        path = out / f"evidence-{len(parts) + 1:02d}.zip"
        path.write_bytes(encoded)
        read_bundle(path)
        parts.append(
            {
                "file": path.name,
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "file_count": len(items),
            }
        )

    encode(sorted(files.items()))
    (out / "index.json").write_text(
        json.dumps({"schema_version": "1.0", "parts": parts, "total_files": len(files)}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return parts


def unpack(index_path, destination):
    index_path, destination = Path(index_path), Path(destination)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema_version") != "1.0" or not isinstance(index.get("parts"), list):
        raise ValueError("invalid evidence index")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("unpack destination must be empty")
    validated, seen = [], set()
    total_size = 0
    for part in index["parts"]:
        if Path(part["file"]).name != part["file"] or "\\" in part["file"]:
            raise ValueError("invalid evidence part name")
        path = index_path.parent / part["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != part["sha256"]:
            raise ValueError("evidence part checksum mismatch")
        manifest = read_bundle(path)
        if seen.intersection(manifest["files"]):
            raise ValueError("duplicate path across evidence parts")
        seen.update(manifest["files"])
        with zipfile.ZipFile(path) as archive:
            total_size += sum(info.file_size for info in archive.infolist())
        if total_size > 100_000_000:
            raise ValueError("combined evidence exceeds 100 MB")
        validated.append((path, manifest))
    if len(seen) != index["total_files"]:
        raise ValueError("evidence index count mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for path, manifest in validated:
        with zipfile.ZipFile(path) as archive:
            for name in manifest["files"]:
                target = (root / name).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("evidence path escaped destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(archive.read(name))
    return len(seen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(f"Verified and unpacked {unpack(args.index, args.out)} evidence files")
