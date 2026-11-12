"""Synthetic guided-CLI round trips from an installed wheel and an external cwd."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed interpreter and owned fixture code
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import agentloop

WORKER = """import json
import sys
from datetime import datetime, timedelta
from agentloop.events import AgentEvent
from agentloop.tracer import AgentTrace

request = json.load(sys.stdin)
mode = sys.argv[1]
elapsed = 100 if mode == "baseline" else 20
incomplete = mode in {"incomplete", "cancelled"}
missing_usage = mode == "missing-usage"
started = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
trace = AgentTrace(
    "installed fixture",
    metadata={
        "synthetic": True,
        "output": "wrong" if mode == "wrong" else "correct",
        "integration": "hermes",
        "hermes": {
            "capture_complete": not incomplete,
            "turn_outcome": "interrupted" if incomplete else "completed",
            "unfinished_requests": 1 if incomplete else 0,
            "events_dropped": 0,
        },
    },
    started_at=started.isoformat(),
    ended_at=(started + timedelta(milliseconds=elapsed)).isoformat(),
    elapsed_ms=elapsed,
)
trace.add_event(AgentEvent(
    event_id="model", run_id=trace.run_id, event_type="model_call", name="fixture",
    started_at=trace.started_at, ended_at=trace.ended_at, duration_ms=elapsed,
    model="gpt-4.1-mini", input_tokens=0 if missing_usage else elapsed,
    output_tokens=0 if missing_usage else 5,
    token_provenance="unavailable" if missing_usage else "provider",
))
trace.export_json(request["trace_path"])
raise SystemExit(130 if mode == "cancelled" else 0)
"""


def smoke(root):
    if not sys.flags.isolated or not Path(agentloop.__file__).resolve().is_relative_to(
        Path(sys.prefix).resolve()
    ):
        raise RuntimeError("Use the installed wheel's Python with -I")
    root.mkdir(parents=True, exist_ok=True)
    worker = root / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    tasks = root / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "Installed fixture",
                "tasks": [{"id": "one", "input": "owned fixture", "expected": "correct"}],
            }
        ),
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTLOOP_")}

    def cli(*args, expected=0):
        result = subprocess.run(  # nosec B603 - fixed installed CLI and owned fixtures
            [
                sys.executable,
                "-I",
                "-c",
                "from agentloop.entrypoint import app; app()",
                *map(str, args),
            ],
            cwd=root,
            env=env,
            check=False,
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode != expected:
            raise RuntimeError(
                f"Installed guided CLI failed ({result.returncode}): {result.stderr[-3000:]}"
            )

    def capture(mode, expected=0):
        cli(
            "improve",
            "capture",
            "--tasks",
            tasks,
            "--out",
            root / mode,
            "--",
            sys.executable,
            "-I",
            worker,
            mode,
            expected=expected,
        )

    capture("baseline")
    for objective in ("latency", "cost"):
        cli(
            "improve",
            "start",
            "--tasks",
            tasks,
            "--baseline",
            root / "baseline/capture.json",
            "--objective",
            objective,
            "--out",
            root / ("session-" + objective),
        )
    for mode in ("candidate", "wrong", "incomplete", "missing-usage", "cancelled"):
        capture(mode, 1 if mode == "cancelled" else 0)
        cli(
            "improve",
            "finish",
            "--session",
            root / "session-latency/session.json",
            "--candidate",
            root / mode / "capture.json",
            "--change",
            "Synthetic fixture comparison",
            "--out",
            root / ("report-" + mode),
            expected=0 if mode in {"candidate", "missing-usage"} else 1,
        )
        report = json.loads((root / ("report-" + mode) / "report.json").read_text(encoding="utf-8"))
        if report["planned_attempts"] != 1 or report["passed"] is not (
            mode in {"candidate", "missing-usage"}
        ):
            raise RuntimeError("Installed guided outcome lost a planned attempt or quality failure")
        if not (root / ("report-" + mode) / "report.html").is_file():
            raise RuntimeError("Installed guided HTML report missing")
    cli(
        "improve",
        "finish",
        "--session",
        root / "session-cost/session.json",
        "--candidate",
        root / "missing-usage/capture.json",
        "--change",
        "Unknown usage",
        "--out",
        root / "report-cost",
        expected=1,
    )
    report = json.loads((root / "report-cost/report.json").read_text(encoding="utf-8"))
    if report["cases"][0]["replay"]["deltas"]["cost_usd_delta"] is not None:
        raise RuntimeError("Missing usage became a measured cost saving")
    cancelled = json.loads((root / "cancelled/capture.json").read_text(encoding="utf-8"))
    if cancelled["runs"][0]["status"] != "cancelled":
        raise RuntimeError("Child cancellation was flattened into failure")
    print(
        "Installed guided CLI preserved success, incorrect answers, incomplete capture, missing usage and cancellation."
    )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="agentloop-guided-wheel-") as temporary:
        smoke(Path(temporary))
