"""Run frozen finding evaluations and optionally enforce a reviewed release baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import (
    compare_finding_benchmarks,
    finding_benchmark_markdown,
    run_finding_benchmark,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--split", choices=("development", "evaluation"), required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--review-ref", help="Explicit review of a labeled regression, retained in gate JSON"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("use a fresh or empty output directory")
    if args.review_ref and not args.baseline:
        parser.error("--review-ref requires --baseline")
    protocol = FindingBenchmark(json.loads(args.corpus.read_text(encoding="utf-8")))
    result = run_finding_benchmark(protocol, split=args.split, source_revision=args.source_revision)
    gate = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        gate = compare_finding_benchmarks(protocol, baseline, result, review_ref=args.review_ref)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.out / "report.md").write_text(
        finding_benchmark_markdown(protocol, result, gate), encoding="utf-8"
    )
    if gate:
        (args.out / "gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
    print(f"{len(result['rows'])} planned cases; report: {args.out / 'report.md'}")
    return 1 if gate and not gate["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
