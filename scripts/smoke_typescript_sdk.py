"""Verify a built TypeScript SDK against the native schema and a local API."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess  # nosec B404 - fixed Node executable and own SDK fixture
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, default=ROOT / "sdk/typescript/dist/index.js")
    parser.add_argument("--out", type=Path, default=ROOT / "runs/typescript-smoke")
    args = parser.parse_args()
    work = args.out.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTLOOP_STORE_BACKEND"] = "sqlite"
    os.environ["AGENTLOOP_SQLITE_PATH"] = str(work / "api.db")
    os.environ["AGENTLOOP_ADMIN_API_KEY"] = "synthetic-" + secrets.token_hex(24)
    os.environ["AGENTLOOP_REQUIRE_API_KEY"] = "true"
    import uvicorn

    from agentloop.server import app
    from agentloop.store import get_store
    from agentloop.tracer import AgentTrace

    get_store().init()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        env = {
            **os.environ,
            "AGENTLOOP_API_URL": f"http://127.0.0.1:{port}",
            "AGENTLOOP_SDK_MODULE": args.module.resolve().as_uri(),
        }
        trace_path = work / "trace.json"
        try:
            subprocess.run(  # nosec B603 B607 - fixed Node command and owned local interoperability script
                ["node", str(ROOT / "sdk/typescript/test/interop.mjs"), str(trace_path)],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=30,
            )
            trace = AgentTrace.from_json(trace_path)
            report = trace.report()
            if len(trace.events) != 2 or report["model_call_count"] != 1:
                raise RuntimeError("TypeScript model/span counts were not preserved")
            if report["input_tokens"] != 12 or report["output_tokens"] != 3:
                raise RuntimeError("TypeScript usage was not preserved")
            if trace.events[0].parent_id != trace.events[1].event_id:
                raise RuntimeError("TypeScript parent relationships were not preserved")
            from agentloop.entrypoint import _analysis_payload
            from agentloop.html_report import analysis_to_html

            (work / "report.html").write_text(
                analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
            )
            print("Python native schema, graph, metrics and HTML interoperability passed.")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Local API smoke server did not stop")


if __name__ == "__main__":
    main()
