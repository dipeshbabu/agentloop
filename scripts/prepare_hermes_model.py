"""Download pinned public CPU inference artifacts for the owned Hermes example."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

ASSETS = {
    "runtime.tar.gz": (
        "https://github.com/ggml-org/llama.cpp/releases/download/b10964/llama-b10964-bin-ubuntu-x64.tar.gz",
        16825086,
        "9abf88aea48a55d0f80edb1ee20220b186848cca0b4e919d71518cfd7ca67443",
    ),
    "model.gguf": (
        "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        491400032,
        "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
    ),
}


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    for name, (url, size, expected) in ASSETS.items():
        path = root / name
        digest, count = hashlib.sha256(), 0
        with urlopen(url, timeout=60) as response, path.open("xb") as target:  # nosec B310 - fixed HTTPS artifact URLs
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > size:
                    raise ValueError("Download exceeded its pinned size")
                digest.update(block)
                target.write(block)
        if count != size or digest.hexdigest() != expected:
            raise ValueError("Inference artifact checksum mismatch")
    runtime = root / "runtime"
    runtime.mkdir()
    with tarfile.open(root / "runtime.tar.gz") as archive:
        archive.extractall(runtime, filter="data")  # nosec B202 - stdlib data filter and pinned verified archive
    servers = list(runtime.rglob("llama-server"))
    if len(servers) != 1:
        raise ValueError("Expected one pinned llama-server executable")
    state = {
        "server": str(servers[0].resolve()),
        "model": str((root / "model.gguf").resolve()),
        "assets": {
            name: {"url": url, "sha256": digest, "bytes": size}
            for name, (url, size, digest) in ASSETS.items()
        },
    }
    (root / "runtime.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    prepare(parser.parse_args().out.resolve())
