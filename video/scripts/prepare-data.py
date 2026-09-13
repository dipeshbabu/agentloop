"""Refresh the video's numbers by running AgentLoop's real offline workflow."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "runs" / "video-evidence"
VIDEO = ROOT / "video"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cli(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from agentloop.entrypoint import app; app()", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "PYTHONUTF8": "1", "NO_COLOR": "1", "COLUMNS": "120"},
    )
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify refreshed facts match the committed snapshot."
    )
    args = parser.parse_args()
    workflow = runpy.run_path(str(ROOT / "examples" / "intervention_study.py"))
    outcomes = workflow["run"](OUT)
    baseline_path = OUT / "baseline.json"
    candidate_path = OUT / "candidate.json"
    baseline_path.write_bytes((OUT / "baseline" / "baseline-task0-seed0.json").read_bytes())
    candidate_path.write_bytes((OUT / "candidate" / "candidate-task0-seed0.json").read_bytes())
    quality = OUT / "quality.json"
    quality.write_text(
        json.dumps({"fixtures": [{"id": "answer", "expected": "ok"}]}), encoding="utf-8"
    )
    transcripts = {}
    transcripts["quickstart"] = cli(
        "quickstart",
        "--out",
        "runs/video-evidence/quickstart.json",
        "--json-out",
        "runs/video-evidence/quickstart-analysis.json",
    )
    transcripts["analyze"] = cli(
        "analyze",
        "runs/video-evidence/baseline.json",
        "--json-out",
        "runs/video-evidence/analysis.json",
    )
    transcripts["replay"] = cli(
        "replay",
        "--baseline",
        "runs/video-evidence/baseline.json",
        "--candidate",
        "runs/video-evidence/candidate.json",
        "--quality-fixtures",
        "runs/video-evidence/quality.json",
        "--out",
        "runs/video-evidence/replay.md",
        "--json-out",
        "runs/video-evidence/replay.json",
    )
    transcripts["html"] = cli(
        "analyze",
        "runs/video-evidence/candidate.json",
        "--baseline",
        "runs/video-evidence/baseline.json",
        "--quality-fixtures",
        "runs/video-evidence/quality.json",
        "--html",
        "runs/video-evidence/report.html",
    )
    transcripts["study"] = cli(
        "study",
        "summarize",
        "runs/video-evidence/study.json",
        "--out",
        "runs/video-evidence/study.md",
        "--json-out",
        "runs/video-evidence/study-results.json",
    )
    analysis = read(OUT / "analysis.json")
    replay = read(OUT / "replay.json")
    quickstart = read(OUT / "quickstart-analysis.json")
    record_paths = sorted((OUT / "interventions").glob("*.json"))
    records = sorted(
        (read(path) for path in record_paths),
        key=lambda record: (record["metadata"]["task_id"], record["metadata"]["seed"]),
    )
    finding = next(
        item for item in analysis["diagnosis"]["findings"] if item["type"] == "parallelize_tools"
    )
    data = {
        "schemaVersion": 1,
        "synthetic": True,
        "sourceCommand": "uv run --frozen python video/scripts/prepare-data.py",
        "sourceCommit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "quickstart": {
            "runtimeMs": quickstart["report"]["total_runtime_ms"],
            "spanCount": quickstart["report"]["event_count"],
            "findingCount": quickstart["diagnosis"]["summary"]["finding_count"],
        },
        "baseline": read(baseline_path),
        "candidate": read(candidate_path),
        "finding": finding,
        "replay": replay,
        "outcomes": outcomes,
        "pairs": [
            {
                "task": record["metadata"]["task_id"],
                "seed": record["metadata"]["seed"],
                "passed": record["gates_passed"],
                "qualityPassed": record["measured"]["quality"]["passed"],
                "costEvaluable": record["measured"]["gates"]["cost_evaluable"],
            }
            for record in records
        ],
    }
    if args.check:
        expected = read(VIDEO / "src" / "demo-data.json")
        expected.pop("sourceCommit")
        comparable = dict(data)
        comparable.pop("sourceCommit")
        if expected != comparable:
            raise SystemExit(
                "Video facts changed; refresh the snapshot and review the on-screen claims."
            )
        if (VIDEO / "public" / "report.html").read_text(encoding="utf-8") != (
            OUT / "report.html"
        ).read_text(encoding="utf-8"):
            raise SystemExit(
                "The report layout changed; refresh the HTML snapshot and report screenshot."
            )
        print("Verified the video snapshot against freshly generated AgentLoop output.")
        return
    (VIDEO / "src").mkdir(parents=True, exist_ok=True)
    (VIDEO / "public").mkdir(exist_ok=True)
    (VIDEO / "src" / "demo-data.json").write_bytes(
        (json.dumps(data, indent=2) + "\n").encode("utf-8")
    )
    transcript = "\n\n".join(f"=== {name} ===\n{output}" for name, output in transcripts.items())
    (VIDEO / "public" / "cli-transcript.txt").write_bytes(
        ("\n".join(line.rstrip() for line in transcript.splitlines()).rstrip() + "\n").encode(
            "utf-8"
        )
    )
    (VIDEO / "public" / "report.html").write_bytes(
        (OUT / "report.html").read_text(encoding="utf-8").encode("utf-8")
    )
    print(
        f"Refreshed video evidence: {replay['baseline']['runtime_ms']:.0f} ms -> {replay['candidate']['runtime_ms']:.0f} ms; {outcomes['configured_gates_passed_count']}/{outcomes['intervention_count']} pairs pass."
    )


if __name__ == "__main__":
    main()
