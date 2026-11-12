"""Exercise the guided CLI with real Hermes and a pinned local CPU model."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess  # nosec B404 - owned pinned runtime and fixed CLI commands
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def cli(*arguments, expected=0):
    result = subprocess.run(  # nosec B603 - fixed AgentLoop entry point and owned paths
        [sys.executable, "-c", "from agentloop.entrypoint import app; app()", *map(str, arguments)],
        cwd=ROOT,
        check=False,
        timeout=1800,
    )
    if result.returncode != expected:
        raise RuntimeError(f"Guided CLI returned {result.returncode}; expected {expected}")


def run(args):
    from agentloop.improvement import _stop_process

    args.out.mkdir(parents=True, exist_ok=False)
    runtime = json.loads(args.model_runtime.read_bytes())
    source = args.out / "pyproject.toml"
    shutil.copyfile(ROOT / "pyproject.toml", source)
    tasks = args.out / "tasks.json"
    shutil.copyfile(ROOT / "examples/hermes_repository_tasks.json", tasks)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "LD_LIBRARY_PATH": str(Path(runtime["server"]).parent)}
    with (args.out / "model-server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(  # nosec B603 - checksum-verified owned llama.cpp executable
            [
                runtime["server"],
                "--model",
                runtime["model"],
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ctx-size",
                "65536",
                "--parallel",
                "1",
                "--threads",
                "2",
                "--alias",
                "agentloop-local-qwen",
                "--jinja",
                "--no-webui",
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            ready = False
            for _ in range(120):
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:  # nosec B310 - owned loopback readiness
                        ready = response.status == 200
                except (URLError, TimeoutError):
                    pass
                if ready:
                    break
                if process.poll() is not None:
                    raise RuntimeError("Local inference server exited before readiness")
                time.sleep(1)
            if not ready:
                raise RuntimeError("Local inference server did not become ready")
            # One declared warm-up, outside every measured task attempt.
            request = Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=json.dumps(
                    {
                        "model": "agentloop-local-qwen",
                        "messages": [{"role": "user", "content": "Reply OK."}],
                        "max_tokens": 4,
                        "temperature": 0,
                        "cache_prompt": False,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=120) as response:  # nosec B310 - owned loopback warm-up
                response.read(1024 * 1024)
            base = f"http://127.0.0.1:{port}/v1"
            runner = [
                str(args.hermes_root / ".venv/bin/python"),
                "-I",
                str(ROOT / "examples/hermes_repository_runner.py"),
                "--base-url",
                base,
                "--hermes-root",
                str(args.hermes_root),
                "--plugin-dir",
                str(args.plugin_dir),
                "--source",
                str(source),
            ]
            cli(
                "improve",
                "capture",
                "--show-runner-output",
                "--tasks",
                tasks,
                "--out",
                args.out / "baseline",
                "--timeout",
                "240",
                "--",
                *runner,
                "--condition",
                "baseline",
            )
            cli(
                "improve",
                "start",
                "--tasks",
                tasks,
                "--baseline",
                args.out / "baseline/capture.json",
                "--focus",
                "add_schema_validation",
                "--out",
                args.out / "session",
            )
            cli(
                "improve",
                "capture",
                "--show-runner-output",
                "--tasks",
                tasks,
                "--out",
                args.out / "candidate",
                "--timeout",
                "240",
                "--",
                *runner,
                "--condition",
                "candidate",
            )
            change = "Request constrained JSON on the first Hermes turn to avoid a second model call repairing the free-text output format. Keep the same independent answer checks."
            cli(
                "improve",
                "finish",
                "--session",
                args.out / "session/session.json",
                "--candidate",
                args.out / "candidate/capture.json",
                "--change",
                change,
                "--out",
                args.out / "results",
            )
            cli(
                "improve",
                "capture",
                "--show-runner-output",
                "--tasks",
                tasks,
                "--out",
                args.out / "wrong",
                "--timeout",
                "240",
                "--",
                *runner,
                "--condition",
                "wrong",
            )
            cli(
                "improve",
                "finish",
                "--session",
                args.out / "session/session.json",
                "--candidate",
                args.out / "wrong/capture.json",
                "--change",
                "Negative control asks for the wrong answer",
                "--out",
                args.out / "rejected",
                expected=1,
            )
            rejected = json.loads((args.out / "rejected/report.json").read_bytes())
            if rejected["passed"] or rejected["passed_attempts"]:
                raise RuntimeError("Incorrect local-model answers passed the quality checks")
            model_requests = 0
            for condition in ("baseline", "candidate", "wrong"):
                manifest = json.loads((args.out / condition / "capture.json").read_bytes())
                for receipt in manifest["runs"]:
                    trace = json.loads((args.out / condition / receipt["trace"]).read_bytes())
                    model_requests += sum(
                        event["event_type"] == "model_call" for event in trace["events"]
                    )
            protocol = {
                "runtime_assets": runtime["assets"],
                "model_requests": model_requests,
                "warmup_requests": 1,
                "paid_provider_requests": 0,
                "decoding": {
                    "temperature": 0,
                    "seed": 20261112,
                    "cache_prompt": False,
                    "response_format": "json_object",
                },
                "order": "All baselines precede the frozen session, candidates and negative controls; fixed order limits causal attribution",
                "scope": "Controlled public repository metadata workload on one pinned local model; measured combined intervention",
            }
            (args.out / "protocol.json").write_text(
                json.dumps(protocol, indent=2) + "\n", encoding="utf-8"
            )
        finally:
            _stop_process(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument("--model-runtime", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.out = arguments.out.resolve()
    arguments.hermes_root = arguments.hermes_root.resolve()
    arguments.plugin_dir = arguments.plugin_dir.resolve()
    arguments.model_runtime = arguments.model_runtime.resolve()
    run(arguments)
