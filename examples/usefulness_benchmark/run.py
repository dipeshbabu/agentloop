"""Freeze, execute, summarize and archive the fixed offline usefulness benchmark."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.real_agent_study.archive import pack, unpack
from examples.usefulness_benchmark.historical import analyze_history
from examples.usefulness_benchmark.protocol import (
    ROOT,
    file_hash,
    freeze,
    load_protocol,
    read,
    write_new,
)
from examples.usefulness_benchmark.reference import execute_references
from examples.usefulness_benchmark.report import export


def execute(protocol_path, out):
    protocol = load_protocol(protocol_path)
    if os.getenv("AGENTLOOP_PRICING_FILE"):
        raise ValueError("this frozen benchmark requires the built-in pricing snapshot")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / "protocol.json", protocol)
    write_new(
        out / "environment.json",
        {"python": sys.version, "platform": platform.platform(), "paid_provider_spend_usd": 0},
    )
    for name in protocol["analyzer"]["sources"]:
        destination = out / "implementation" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_text(encoding="utf-8").encode("utf-8"))
    for name in ("LICENSE", "pyproject.toml", "uv.lock", "THIRD_PARTY_LICENSES.md"):
        destination = out / "implementation" / name
        destination.write_bytes((ROOT / name).read_bytes())
    source_roots = {}
    for label, field, expected in (
        ("real-agent", "real_agent_index", "research/real-agent-2026-09/index.json"),
        ("calibration", "calibration_index", "research/empirical-calibration-2026-09/index.json"),
    ):
        declaration = protocol["historical"][field]
        if declaration["path"] != expected or file_hash(ROOT / expected) != declaration["sha256"]:
            raise ValueError("historical evidence index differs from protocol")
        source_roots[label] = out / "historical-sources" / label
        unpack(ROOT / expected, source_roots[label])
    reference = execute_references(protocol, out)
    historical = analyze_history(
        protocol, source_roots["real-agent"], source_roots["calibration"], out
    )
    result = export(out, out / "results.json", out / "REPORT.md")
    print(
        json.dumps(
            {
                "protocol_sha256": protocol["sha256"],
                "reference_pairs": len(reference["rows"]),
                "historical_pairs": len(historical["rows"]),
                "retired_unmatched": len(historical["retired_onboarding"]["rows"]),
                "workload_rows": len(result["workload_results"]),
            }
        )
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--out", type=Path, required=True)
    freeze_parser.add_argument("--source-revision", required=True)
    freeze_parser.add_argument("--repetitions", type=int, default=2)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("protocol", type=Path)
    execute_parser.add_argument("--out-dir", type=Path, required=True)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("root", type=Path)
    report_parser.add_argument("--json-out", type=Path, required=True)
    report_parser.add_argument("--markdown-out", type=Path, required=True)
    pack_parser = sub.add_parser("pack")
    pack_parser.add_argument("root", type=Path)
    pack_parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        print(
            freeze(args.out, source_revision=args.source_revision, repetitions=args.repetitions)[
                "sha256"
            ]
        )
    elif args.command == "execute":
        execute(args.protocol, args.out_dir)
    elif args.command == "report":
        result = export(args.root, args.json_out, args.markdown_out)
        print(result["sha256"])
    else:
        # Revalidate the retained inputs before publishing the archive.
        from examples.usefulness_benchmark.report import summarize

        result = summarize(args.root)
        if result != read(args.root / "results.json"):
            raise ValueError("retained result differs from offline reconstruction")
        files = {
            path.relative_to(args.root).as_posix(): path.read_bytes()
            for path in args.root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        print(json.dumps(pack(files, args.out_dir)))


if __name__ == "__main__":
    main()
