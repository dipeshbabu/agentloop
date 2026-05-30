from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentloop.tracer import AgentTrace


class AgentLoopClientError(RuntimeError):
    pass


@dataclass
class AgentLoopClient:
    """Tiny dependency-free client for sending traces to an AgentLoop API server."""

    base_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    admin_api_key: str | None = None
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> "AgentLoopClient":
        return cls(
            base_url=os.getenv("AGENTLOOP_API_URL", "http://127.0.0.1:8000"),
            api_key=os.getenv("AGENTLOOP_API_KEY"),
            admin_api_key=os.getenv("AGENTLOOP_ADMIN_API_KEY"),
            timeout_s=float(os.getenv("AGENTLOOP_TIMEOUT_S", "10")),
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_api_key(self, project_id: str = "default", name: str = "default") -> dict[str, Any]:
        return self._request("POST", "/api-keys", {"project_id": project_id, "name": name})

    def upload_trace(self, trace: AgentTrace | str | Path | dict[str, Any]) -> dict[str, Any]:
        if isinstance(trace, AgentTrace):
            payload = trace.to_dict()
        elif isinstance(trace, dict):
            payload = trace
        else:
            payload = json.loads(Path(trace).read_text(encoding="utf-8"))
        return self._request("POST", "/traces", payload)

    def list_traces(self, project_id: str | None = None) -> dict[str, Any]:
        route = "/traces"
        if project_id:
            route += "?" + urllib.parse.urlencode({"project_id": project_id})
        return self._request("GET", route)

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        route = "/usage"
        if project_id:
            route += "?" + urllib.parse.urlencode({"project_id": project_id})
        return self._request("GET", route)

    def get_report(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/traces/{run_id}/report")

    def get_optimization_plan(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/traces/{run_id}/optimize")

    def get_diagnosis(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/traces/{run_id}/diagnose")

    def get_value_report(
        self,
        run_id: str,
        *,
        runs_per_month: int | None = None,
        engineer_hourly_rate_usd: float | None = None,
        incident_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if runs_per_month is not None:
            params["runs_per_month"] = runs_per_month
        if engineer_hourly_rate_usd is not None:
            params["engineer_hourly_rate_usd"] = engineer_hourly_rate_usd
        if incident_cost_usd is not None:
            params["incident_cost_usd"] = incident_cost_usd
        route = f"/traces/{run_id}/value"
        if params:
            route += "?" + urllib.parse.urlencode(params)
        return self._request("GET", route)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-AgentLoop-Key"] = self.api_key
        if self.admin_api_key:
            headers["X-AgentLoop-Admin-Key"] = self.admin_api_key
        req = urllib.request.Request(url=url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise AgentLoopClientError(f"AgentLoop API returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentLoopClientError(f"Could not reach AgentLoop API at {url}: {exc.reason}") from exc
