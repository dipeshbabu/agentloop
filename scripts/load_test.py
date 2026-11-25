"""Load a real AgentLoop server with concurrent uploads, reads and a retention purge.

Starts uvicorn with several worker processes on a fresh store, then for
--seconds runs upload threads, read threads and a purge of a separate seeded
project at the same time. Reports throughput, latency percentiles and every
error. Exits 1 on any failed request or purge, or when --min-uploads-per-second
is not met.

    uv run --all-extras python scripts/load_test.py --workers 4 --uploaders 8

Set AGENTLOOP_STORE_BACKEND=postgres and AGENTLOOP_DATABASE_URL to load an
empty Postgres database instead of a temporary SQLite file.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import statistics
import subprocess  # nosec B404 - starts and stops its own uvicorn server with fixed argv
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_scale import _trace  # noqa: E402

PROJECT = "load"
PURGED_PROJECT = "expired"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: dict[str, list[float]] = {}
        self.errors: Counter[str] = Counter()

    def record(self, name: str, seconds: float, error: str | None) -> None:
        with self.lock:
            self.latencies.setdefault(name, []).append(seconds)
            if error:
                self.errors[f"{name}: {error}"] += 1


def _request(conn, method: str, path: str, body: bytes | None, headers: dict) -> str | None:
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        return type(exc).__name__
    if response.status >= 400:
        detail = payload[:200].decode("utf-8", "replace")
        return f"HTTP {response.status} {detail}"
    return None


def _uploader(port, key, stop, recorder, bodies) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"X-AgentLoop-Key": key, "Content-Type": "application/json"}
    index = 0
    while not stop.is_set():
        body = bodies[index % len(bodies)]
        # A fresh run_id per upload (in the trace and its events) so each
        # request is a real insert.
        run_id = json.loads(body)["run_id"]
        unique = body.replace(run_id, f"{run_id}-{threading.get_ident()}-{index}")
        started = time.perf_counter()
        error = _request(conn, "POST", "/v1/traces", unique.encode(), headers)
        recorder.record("upload", time.perf_counter() - started, error)
        index += 1
    conn.close()


READ_PATHS = (
    "/v1/traces?page_size=50",
    "/v1/usage",
    "/v1/findings?page_size=50",
    "/v1/optimization-queue",
    "/v1/ci-runs",
)


def _reader(port, key, stop, recorder) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"X-AgentLoop-Key": key}
    index = 0
    while not stop.is_set():
        path = READ_PATHS[index % len(READ_PATHS)]
        started = time.perf_counter()
        error = _request(conn, "GET", path, None, headers)
        recorder.record("read " + path.split("?")[0], time.perf_counter() - started, error)
        index += 1
    conn.close()


def _purger(store, recorder, result: dict) -> None:
    started = time.perf_counter()
    try:
        result.update(store.purge_traces(1 / 86400, project_id=PURGED_PROJECT, actor="load-test"))
        error = None
    except Exception as exc:  # report every failure, whatever its type
        error = f"{type(exc).__name__}: {exc}"
    recorder.record("purge", time.perf_counter() - started, error)


def _wait_for_server(port: int, server: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"server exited with code {server.returncode}")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not become healthy")


def _stop_server(server: subprocess.Popen) -> None:
    """Stop uvicorn and its worker processes."""
    if os.name == "nt":
        # terminate() would end only the parent and orphan the workers.
        subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed taskkill argv, own child PID
            ["taskkill", "/T", "/F", "/PID", str(server.pid)],
            capture_output=True,
            check=False,
        )
    else:
        server.terminate()  # uvicorn stops its workers on SIGTERM
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=4, help="uvicorn worker processes")
    parser.add_argument("--uploaders", type=int, default=8, help="concurrent upload clients")
    parser.add_argument("--readers", type=int, default=4, help="concurrent read clients")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--seed-expired", type=int, default=500, help="traces for the purge")
    parser.add_argument("--min-uploads-per-second", type=float, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Write results as JSON.")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="agentloop-load-"))
    env = dict(os.environ)
    if env.get("AGENTLOOP_STORE_BACKEND", "sqlite") == "sqlite":
        env["AGENTLOOP_STORE_BACKEND"] = "sqlite"
        env.setdefault("AGENTLOOP_SQLITE_PATH", str(workdir / "load.db"))
    env.pop("AGENTLOOP_REQUIRE_API_KEY", None)
    os.environ.update(env)

    from agentloop.store import get_store

    store = get_store()
    store.init()
    key = store.create_api_key(PROJECT, "load test")["api_key"]
    for index in range(args.seed_expired):
        store.save_trace(_trace(index), project_id=PURGED_PROJECT)
    bodies = [json.dumps(_trace(index).to_dict()) for index in range(20)]

    port = _free_port()
    server = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed uvicorn argv via sys.executable
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agentloop.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(args.workers),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )
    recorder = Recorder()
    purge_result: dict = {}
    try:
        _wait_for_server(port, server)
        stop = threading.Event()
        threads = [
            threading.Thread(target=_uploader, args=(port, key, stop, recorder, bodies))
            for _ in range(args.uploaders)
        ] + [
            threading.Thread(target=_reader, args=(port, key, stop, recorder))
            for _ in range(args.readers)
        ]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        time.sleep(min(2.0, args.seconds / 4))  # purge while the load is running
        purger = threading.Thread(target=_purger, args=(store, recorder, purge_result))
        purger.start()
        time.sleep(max(0.0, args.seconds - (time.perf_counter() - started)))
        stop.set()
        for thread in [*threads, purger]:
            thread.join()
        elapsed = time.perf_counter() - started
    finally:
        _stop_server(server)

    stored = len(store.list_traces(project_id=PROJECT))
    uploads = len(recorder.latencies.get("upload", []))
    upload_errors = sum(
        count for name, count in recorder.errors.items() if name.startswith("upload")
    )
    rate = (uploads - upload_errors) / elapsed
    backend = env["AGENTLOOP_STORE_BACKEND"]
    print(
        f"{backend}: {args.workers} workers, {args.uploaders} uploaders, "
        f"{args.readers} readers, {elapsed:.1f}s"
    )
    rows = []
    for name, values in sorted(recorder.latencies.items()):
        row = {
            "name": name,
            "requests": len(values),
            "p50_ms": round(statistics.median(values) * 1000, 1),
            "p95_ms": round(_percentile(values, 0.95) * 1000, 1),
            "max_ms": round(max(values) * 1000, 1),
        }
        rows.append(row)
        print(
            f"  {name:<28} {row['requests']:>6}  p50 {row['p50_ms']:>8.1f} ms  "
            f"p95 {row['p95_ms']:>8.1f} ms  max {row['max_ms']:>8.1f} ms"
        )
    print(f"  uploads/second: {rate:.1f}; stored {stored} of {uploads - upload_errors} accepted")
    print(f"  purge: {purge_result}")

    failures = list(recorder.errors.items())
    if stored != uploads - upload_errors:
        failures.append((f"stored {stored} traces but {uploads - upload_errors} were accepted", 1))
    if purge_result.get("traces", 0) != args.seed_expired:
        failures.append((f"purge removed {purge_result} of {args.seed_expired}", 1))
    if args.min_uploads_per_second is not None and rate < args.min_uploads_per_second:
        failures.append((f"{rate:.1f} uploads/s is below {args.min_uploads_per_second}", 1))
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "backend": backend,
                    "uploads_per_second": round(rate, 1),
                    "latency": rows,
                    "purge": purge_result,
                    "failures": [f"{name} (x{count})" for name, count in failures],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    for name, count in failures:
        print(f"FAILED: {name} (x{count})", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
