"""Time AgentLoop's store, API and dashboard against a store of realistic size.

Fills a fresh store with traces that produce findings and carry CI labels, then
times each store query, API route and dashboard page. Use --max-seconds to fail
when any measured operation is slower, for example in CI.

    uv run --all-extras python scripts/benchmark_scale.py --traces 2000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "scale"


def _trace(index: int):
    from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call

    run = index // 2
    with trace_agent(
        name=f"agent-{index % 7}",
        metadata={
            "labels": {
                "workflow_run": str(1000 + run),
                "condition": "baseline" if index % 2 == 0 else "candidate",
                "commit": f"{run:07x}",
                "ref": "main",
                "repository": "team/app",
            }
        },
    ) as trace:
        context = "stable instructions " * 60
        for _ in range(3):
            with trace_model_call("plan", input_text=context, output_tokens=40):
                pass
        with trace_tool_call("search"):
            pass
    return trace


def populate(store, traces: int, other_projects: int) -> float:
    started = time.perf_counter()
    for index in range(traces):
        store.save_trace(_trace(index), project_id=PROJECT)
    for project in range(other_projects):
        for index in range(max(1, traces // 10)):
            store.save_trace(_trace(index), project_id=f"other-{project}")
    return time.perf_counter() - started


@contextmanager
def _timer(results: list[dict], area: str, name: str):
    started = time.perf_counter()
    detail: dict = {}
    yield detail
    results.append(
        {"area": area, "name": name, "seconds": round(time.perf_counter() - started, 3), **detail}
    )


def measure_store(store, results: list[dict]) -> None:
    from agentloop.ci_runs import ci_run_pair, list_ci_runs

    with _timer(results, "store", "list_traces (unpaginated)") as detail:
        detail["rows"] = len(store.list_traces(project_id=PROJECT))
    with _timer(results, "store", "list_traces_page (50)") as detail:
        detail["rows"] = len(store.list_traces_page(project_id=PROJECT, limit=50)["items"])
    with _timer(results, "store", "list_traces_page by label") as detail:
        page = store.list_traces_page(project_id=PROJECT, limit=50, label=("commit", "0000001"))
        detail["rows"] = len(page["items"])
    with _timer(results, "store", "usage_summary"):
        store.usage_summary(project_id=PROJECT)
    with _timer(results, "store", "optimization_queue") as detail:
        detail["rows"] = len(store.optimization_queue(project_id=PROJECT))
    with _timer(results, "store", "list_findings_page (50)") as detail:
        detail["rows"] = len(store.list_findings_page(project_id=PROJECT, limit=50)["items"])
    with _timer(results, "store", "list_ci_runs (scan 500)") as detail:
        detail["rows"] = len(list_ci_runs(store, PROJECT))
    with _timer(results, "store", "ci_run_pair"):
        ci_run_pair(store, PROJECT, "1001")
    with _timer(results, "store", "list_audit_events (100)"):
        store.list_audit_events(PROJECT, limit=100)


def measure_api(store, results: list[dict]) -> None:
    from fastapi.testclient import TestClient

    from agentloop.server import app

    api_key = store.create_api_key(PROJECT, "scale benchmark")["api_key"]
    client = TestClient(app, headers={"X-AgentLoop-Key": api_key})
    routes = {
        "GET /v1/traces (unpaginated)": "/v1/traces",
        "GET /v1/traces (page 50)": "/v1/traces?page_size=50",
        "GET /v1/traces (label)": "/v1/traces?label=commit=0000001",
        "GET /v1/usage": "/v1/usage",
        "GET /v1/optimization-queue": "/v1/optimization-queue",
        "GET /v1/findings (page 50)": "/v1/findings?page_size=50",
        "GET /v1/ci-runs": "/v1/ci-runs",
        "GET /v1/ci-runs/1001/comparison": "/v1/ci-runs/1001/comparison",
    }
    uploads = [_trace(index).to_dict() for index in range(200)]
    with _timer(results, "api", "POST /v1/traces x200") as detail:
        statuses = {client.post("/v1/traces", json=body).status_code for body in uploads}
        detail["status"] = max(statuses)
    for name, route in routes.items():
        with _timer(results, "api", name) as detail:
            response = client.get(route)
            detail["status"] = response.status_code
            detail["bytes"] = len(response.content)


def measure_dashboard(results: list[dict], timeout: float) -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=timeout)
    # The first run imports Streamlit and the dashboard, so it is reported but
    # not held to --max-seconds.
    with _timer(results, "dashboard", "first load (Overview)") as detail:
        app.run()
        detail["error"] = bool(app.exception)
        detail["warmup"] = True
    next(w for w in app.sidebar.text_input if w.label == "Project").input(PROJECT).run()
    for page in list(app.sidebar.radio[0].options):
        with _timer(results, "dashboard", page) as detail:
            app.sidebar.radio[0].set_value(page).run()
            detail["error"] = bool(app.exception)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--traces", type=int, default=2000)
    parser.add_argument("--other-projects", type=int, default=2)
    parser.add_argument("--out", type=Path, default=None, help="Write results as JSON.")
    parser.add_argument("--max-seconds", type=float, default=None, help="Fail above this time.")
    parser.add_argument(
        "--max-response-mb", type=float, default=None, help="Fail above this API response size."
    )
    parser.add_argument("--skip-dashboard", action="store_true")
    parser.add_argument("--dashboard-timeout", type=float, default=600)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="agentloop-scale-"))
    if os.getenv("AGENTLOOP_STORE_BACKEND", "sqlite") == "sqlite":
        os.environ["AGENTLOOP_STORE_BACKEND"] = "sqlite"
        os.environ.setdefault("AGENTLOOP_SQLITE_PATH", str(workdir / "scale.db"))
    os.environ.pop("AGENTLOOP_REQUIRE_API_KEY", None)
    os.environ.pop("AGENTLOOP_DASHBOARD_REQUIRE_KEY", None)
    os.environ.setdefault("AGENTLOOP_DASHBOARD_RUNS_DIR", str(workdir / "runs"))

    from agentloop.store import get_store

    store = get_store()
    store.init()
    seconds = populate(store, args.traces, args.other_projects)
    print(f"Stored {args.traces} traces in project {PROJECT!r} in {seconds:.1f}s")

    results: list[dict] = []
    measure_store(store, results)
    measure_api(store, results)
    if not args.skip_dashboard:
        measure_dashboard(results, args.dashboard_timeout)

    width = max(len(item["name"]) for item in results)
    for item in results:
        extra = ", ".join(
            f"{key}={value}"
            for key, value in item.items()
            if key not in {"area", "name", "seconds", "warmup"}
        )
        print(f"{item['area']:<9} {item['name']:<{width}} {item['seconds']:>8.3f}s  {extra}")
    if args.out:
        args.out.write_text(
            json.dumps({"traces": args.traces, "results": results}, indent=2), encoding="utf-8"
        )
    failures = [item for item in results if item.get("error") or item.get("status", 200) >= 400]
    if args.max_seconds is not None:
        failures += [
            item
            for item in results
            if item["seconds"] > args.max_seconds and not item.get("warmup")
        ]
    if args.max_response_mb is not None:
        limit = args.max_response_mb * 1024 * 1024
        failures += [item for item in results if item.get("bytes", 0) > limit]
    if failures:
        for item in failures:
            print(f"FAILED: {item['area']} {item['name']} ({item['seconds']}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
