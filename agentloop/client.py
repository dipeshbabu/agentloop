from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agentloop.tracer import AgentTrace

CredentialScope = Literal["project", "admin"]


class AgentLoopClientError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Project and admin credentials must never be forwarded to another route.
        return None


def _open_request(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


@dataclass
class AgentLoopClient:
    """Dependency-free API v1 client accepting an origin, mount path, or /v1 base."""

    base_url: str = "http://127.0.0.1:8000"
    api_key: str | None = field(default=None, repr=False)
    admin_api_key: str | None = field(default=None, repr=False)
    timeout_s: float = 10.0
    max_response_bytes: int = 16 * 1024 * 1024

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
        return self._request(
            "POST",
            "/api-keys",
            {"project_id": project_id, "name": name},
            credential_scope="admin",
        )

    def upload_trace(self, trace: AgentTrace | str | Path | dict[str, Any]) -> dict[str, Any]:
        if isinstance(trace, AgentTrace):
            payload = trace.to_dict()
        elif isinstance(trace, dict):
            payload = trace
        else:
            payload = json.loads(Path(trace).read_text(encoding="utf-8"))
        return self._request("POST", "/traces", payload)

    def list_traces(
        self,
        project_id: str | None = None,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if page_size is not None:
            params["page_size"] = page_size
        if cursor:
            params["cursor"] = cursor
        route = "/traces"
        if params:
            route += "?" + urllib.parse.urlencode(params)
        return self._request("GET", route)

    def usage_summary(self, project_id: str | None = None) -> dict[str, Any]:
        route = "/usage"
        if project_id:
            route += "?" + urllib.parse.urlencode({"project_id": project_id})
        return self._request("GET", route)

    def get_report(self, run_id: str) -> dict[str, Any]:
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        return self._request("GET", f"/traces/{encoded_run_id}/report")

    def get_optimization_plan(self, run_id: str) -> dict[str, Any]:
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        return self._request("GET", f"/traces/{encoded_run_id}/optimization")

    def get_diagnosis(self, run_id: str) -> dict[str, Any]:
        """Compute a diagnosis without changing the server's stored findings."""
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        return self._request("GET", f"/traces/{encoded_run_id}/diagnosis")

    def save_diagnosis(self, run_id: str) -> dict[str, Any]:
        """Recompute and persist a stored trace's diagnosis on the server."""
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        return self._request("POST", f"/traces/{encoded_run_id}/diagnosis")

    def list_findings(
        self,
        project_id: str | None = None,
        status: str | None = None,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if status:
            params["status"] = status
        if page_size is not None:
            params["page_size"] = page_size
        if cursor:
            params["cursor"] = cursor
        route = "/findings"
        if params:
            route += "?" + urllib.parse.urlencode(params)
        return self._request("GET", route)

    def update_finding_status(self, run_id: str, finding_id: str, status: str) -> dict[str, Any]:
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        encoded_finding_id = urllib.parse.quote(finding_id, safe="")
        return self._request(
            "POST", f"/findings/{encoded_run_id}/{encoded_finding_id}/status", {"status": status}
        )

    def optimization_queue(self, project_id: str | None = None) -> dict[str, Any]:
        route = "/optimization-queue"
        if project_id:
            route += "?" + urllib.parse.urlencode({"project_id": project_id})
        return self._request("GET", route)

    def github_issue_drafts(self, project_id: str | None = None, limit: int = 5) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if project_id:
            params["project_id"] = project_id
        return self._request(
            "GET", "/optimization-queue/github-issues?" + urllib.parse.urlencode(params)
        )

    def build_quality_report(
        self,
        fixtures: list[dict[str, Any]],
        *,
        baseline_run_id: str | None = None,
        candidate_run_id: str | None = None,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/quality-reports",
            {
                "fixtures": fixtures,
                "baseline_run_id": baseline_run_id,
                "candidate_run_id": candidate_run_id,
                "min_score": min_score,
            },
        )

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
        encoded_run_id = urllib.parse.quote(run_id, safe="")
        route = f"/traces/{encoded_run_id}/value"
        if params:
            route += "?" + urllib.parse.urlencode(params)
        return self._request("GET", route)

    def create_intervention(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/interventions", request)

    def get_intervention(self, intervention_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(intervention_id, safe="")
        return self._request("GET", f"/interventions/{encoded_id}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        credential_scope: CredentialScope = "project",
    ) -> dict[str, Any]:
        try:
            parsed_base_url = urllib.parse.urlsplit(self.base_url)
            port = parsed_base_url.port
        except (TypeError, ValueError):
            raise AgentLoopClientError("AgentLoop API URL must be a valid HTTP(S) URL") from None
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise AgentLoopClientError("AgentLoop API URL must use http:// or https://")
        if (
            parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise AgentLoopClientError(
                "AgentLoop API URL must not contain credentials, query, or fragment"
            )
        if any(ord(char) <= 32 or ord(char) == 127 for char in self.base_url):
            raise AgentLoopClientError(
                "AgentLoop API URL must not contain whitespace or control characters"
            )
        del port  # Access above validates malformed or out-of-range ports.
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise AgentLoopClientError("timeout_s must be a positive finite number")
        try:
            timeout = float(self.timeout_s)
        except OverflowError:
            raise AgentLoopClientError("timeout_s must be a positive finite number") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise AgentLoopClientError("timeout_s must be a positive finite number")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
        ):
            raise AgentLoopClientError("max_response_bytes must be a positive integer")
        prefix = self.base_url.rstrip("/")
        if prefix.endswith("/v1"):
            prefix = prefix[:-3]
        url = prefix + "/v1" + path
        try:
            body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise AgentLoopClientError(
                "AgentLoop request payload must contain finite JSON data"
            ) from None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if credential_scope == "project":
            if self.api_key:
                headers["X-AgentLoop-Key"] = self.api_key
        elif credential_scope == "admin":
            if self.admin_api_key:
                headers["X-AgentLoop-Admin-Key"] = self.admin_api_key
        else:
            raise AgentLoopClientError(f"Unsupported credential scope: {credential_scope}")
        for key in (self.api_key, self.admin_api_key):
            if key is not None and (
                not isinstance(key, str) or any(ord(char) < 32 or ord(char) == 127 for char in key)
            ):
                raise AgentLoopClientError(
                    "AgentLoop API credentials must be strings without control characters"
                )
        try:
            req = urllib.request.Request(url=url, data=body, method=method, headers=headers)
            with _open_request(req, timeout=timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise AgentLoopClientError(
                        "AgentLoop response exceeds the configured size limit"
                    )
                if getattr(response, "status", None) == 204:
                    return {}
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise AgentLoopClientError("AgentLoop response is not valid UTF-8 JSON") from None
            if not isinstance(result, dict):
                raise AgentLoopClientError("AgentLoop response must be a JSON object")
            return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise AgentLoopClientError(f"AgentLoop API returned HTTP {status}") from None
        except (OSError, ValueError):
            raise AgentLoopClientError("Could not reach the AgentLoop API") from None
