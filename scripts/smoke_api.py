from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _request(
    method: str,
    path: str,
    *,
    api_url: str,
    payload: dict[str, Any] | None = None,
    api_key: str | None = None,
    admin_api_key: str | None = None,
) -> dict[str, Any]:
    parsed_api_url = urllib.parse.urlsplit(api_url)
    if parsed_api_url.scheme not in {"http", "https"} or not parsed_api_url.netloc:
        raise RuntimeError("AGENTLOOP_API_URL must use http:// or https://")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-AgentLoop-Key"] = api_key
    if admin_api_key:
        headers["X-AgentLoop-Admin-Key"] = admin_api_key

    request = urllib.request.Request(
        url=api_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(  # nosec B310 - API URL scheme is validated above.
        request, timeout=10
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    api_url = os.getenv("AGENTLOOP_API_URL", "http://127.0.0.1:8000")
    api_key = os.getenv("AGENTLOOP_API_KEY")
    admin_api_key = os.getenv("AGENTLOOP_ADMIN_API_KEY")

    health = _request("GET", "/health", api_url=api_url)
    if health.get("status") != "ok":
        raise RuntimeError(f"Unexpected health response: {health}")

    ready = _request("GET", "/readyz", api_url=api_url)
    if ready.get("status") != "ready":
        raise RuntimeError(f"Unexpected readiness response: {ready}")

    if not api_key and admin_api_key:
        key = _request(
            "POST",
            "/api-keys",
            api_url=api_url,
            admin_api_key=admin_api_key,
            payload={"project_id": "smoke", "name": "smoke"},
        )
        api_key = str(key["api_key"])

    run_id = f"smoke-{int(time.time())}"
    trace = {
        "name": "production-smoke",
        "run_id": run_id,
        "metadata": {"source": "scripts/smoke_api.py"},
        "events": [],
    }
    uploaded = _request("POST", "/traces", api_url=api_url, api_key=api_key, payload=trace)
    if uploaded.get("run_id") != run_id:
        raise RuntimeError(f"Unexpected upload response: {uploaded}")

    report = _request("GET", f"/traces/{run_id}/report", api_url=api_url, api_key=api_key)
    if "total_runtime_ms" not in report:
        raise RuntimeError(f"Unexpected report response: {report}")

    stored = _request("GET", "/traces", api_url=api_url, api_key=api_key)
    traces = stored.get("traces")
    if not isinstance(traces, list) or not any(item.get("run_id") == run_id for item in traces):
        raise RuntimeError("Uploaded smoke trace was not returned by the list API")

    print(f"AgentLoop API smoke check passed for {api_url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"AgentLoop API smoke check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
