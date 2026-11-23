from __future__ import annotations

import importlib
import os
import platform
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from agentloop.config import postgres_connection_source
from agentloop.runtime import get_runtime_config
from agentloop.store import get_store


@dataclass
class DoctorCheck:
    name: str
    status: str
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.fix:
            payload["fix"] = self.fix
        return payload


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="ok", detail=detail)


def _warn(name: str, detail: str, fix: str | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status="warn", detail=detail, fix=fix)


def _fail(name: str, detail: str, fix: str | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status="fail", detail=detail, fix=fix)


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_check(api_url: str, timeout: float = 2.0) -> DoctorCheck:
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _warn(
            "api_health",
            "AGENTLOOP_API_URL must use http:// or https://",
            "Set AGENTLOOP_API_URL to the AgentLoop HTTP API origin.",
        )
    url = api_url.rstrip("/") + "/health"
    try:
        request = Request(url, headers={"User-Agent": "agentloop-doctor"})
        with urlopen(  # nosec B310 - API URL scheme is validated above.
            request, timeout=timeout
        ) as response:
            if response.status == 200:
                return _ok("api_health", f"{url} returned HTTP 200")
            return _warn("api_health", f"{url} returned HTTP {response.status}")
    except URLError as exc:
        return _warn(
            "api_health",
            f"Could not reach {url}: {exc.reason}",
            "Start the API with: agentloop server --host 127.0.0.1 --port 8000",
        )
    except Exception as exc:  # pragma: no cover - defensive for platform SSL/socket edge cases
        return _warn("api_health", f"Could not reach {url}: {exc}")


def _ready_check(api_url: str, timeout: float = 2.0) -> DoctorCheck:
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _fail(
            "api_ready",
            "AGENTLOOP_API_URL must use http:// or https://",
            "Set AGENTLOOP_API_URL to the deployed AgentLoop HTTP API origin.",
        )
    url = api_url.rstrip("/") + "/readyz"
    try:
        request = Request(url, headers={"User-Agent": "agentloop-production-check"})
        with urlopen(  # nosec B310 - API URL scheme is validated above.
            request, timeout=timeout
        ) as response:
            if response.status == 200:
                return _ok("api_ready", f"{url} returned HTTP 200")
            return _fail("api_ready", f"{url} returned HTTP {response.status}")
    except URLError as exc:
        return _fail(
            "api_ready",
            f"Could not reach {url}: {exc.reason}",
            "Deploy the API and confirm /readyz is reachable.",
        )
    except Exception as exc:  # pragma: no cover - defensive for platform SSL/socket edge cases
        return _fail("api_ready", f"Could not reach {url}: {exc}")


def _is_local_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _hosting_policy_checks() -> list[DoctorCheck]:
    """Checks for settings added for shared deployments (0.2.7 and newer)."""
    from agentloop.request_limits import ENVIRONMENT_VARIABLE, max_request_bytes

    checks: list[DoctorCheck] = []
    try:
        limit = max_request_bytes()
    except ValueError as exc:
        checks.append(_fail("request_size_limit", str(exc), f"Fix {ENVIRONMENT_VARIABLE}."))
    else:
        if limit is None:
            checks.append(
                _warn(
                    "request_size_limit",
                    "disabled; request bodies of any size are read into memory",
                    f"Unset {ENVIRONMENT_VARIABLE} for the 16 MiB default or set a size.",
                )
            )
        else:
            checks.append(_ok("request_size_limit", f"{limit} bytes"))

    if _env_enabled("AGENTLOOP_DASHBOARD_REQUIRE_KEY"):
        checks.append(_ok("dashboard_access", "project API key sign-in required"))
    else:
        checks.append(
            _warn(
                "dashboard_access",
                "the dashboard shows any project to anyone who can reach it",
                "Set AGENTLOOP_DASHBOARD_REQUIRE_KEY=true on the dashboard service.",
            )
        )

    retention = os.getenv("AGENTLOOP_RETENTION_DAYS", "").strip()
    if not retention:
        checks.append(
            _warn(
                "retention",
                "not configured; traces are kept until deleted by hand",
                "Set AGENTLOOP_RETENTION_DAYS and run agentloop purge-traces on a schedule.",
            )
        )
    else:
        try:
            days = float(retention)
        except ValueError:
            days = -1.0
        if days > 0 and days == days and days != float("inf"):
            checks.append(_ok("retention", f"{days:g} days"))
        else:
            checks.append(
                _fail(
                    "retention",
                    f"AGENTLOOP_RETENTION_DAYS={retention!r} is not a positive number of days",
                    "Set it to a positive number of days, or unset it.",
                )
            )
    return checks


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def run_doctor(*, check_api: bool = True, runs_dir: Path | str = "runs") -> dict[str, Any]:
    """Run local setup checks, probing hosted wiring only when it is configured."""

    checks: list[DoctorCheck] = []

    py_version = sys.version_info
    if py_version >= (3, 11):
        checks.append(_ok("python", f"Python {platform.python_version()}"))
    else:
        checks.append(
            _fail(
                "python",
                f"Python {platform.python_version()} is too old",
                "Use Python 3.11 or newer.",
            )
        )

    # These are the actual core runtime dependencies in pyproject.toml. Server,
    # dashboard, Postgres, and instrumentation dependencies are checked by their
    # corresponding modes instead of making a minimal local install look broken.
    for module_name in ["typer", "rich"]:
        try:
            importlib.import_module(module_name)
            checks.append(_ok(f"dependency:{module_name}", "importable"))
        except ImportError:
            checks.append(
                _fail(
                    f"dependency:{module_name}",
                    "not installed",
                    "Install the package with: python -m pip install agentloop-profiler",
                )
            )

    out_dir = Path(runs_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".agentloop_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(_ok("runs_dir", f"{out_dir} is writable"))
    except Exception as exc:
        checks.append(
            _fail(
                "runs_dir",
                f"{out_dir} is not writable: {exc}",
                "Choose a writable path or set AGENTLOOP_EXPORT_DIR.",
            )
        )

    try:
        db = get_store()
        db.init()
        checks.append(_ok("store", f"{db.__class__.__name__} initialized"))
    except Exception as exc:
        checks.append(
            _fail(
                "store",
                f"store initialization failed: {exc}",
                "Check AGENTLOOP_STORE_BACKEND and database credentials.",
            )
        )

    cfg = get_runtime_config()
    auth_required = _env_enabled("AGENTLOOP_REQUIRE_API_KEY")
    explicit_api_url = os.getenv("AGENTLOOP_API_URL")

    if cfg.auto_upload and auth_required and not cfg.api_key:
        checks.append(
            _warn(
                "api_key",
                "auto upload is enabled but AGENTLOOP_API_KEY is empty",
                "Create a key with: agentloop create-api-key --project-id demo",
            )
        )
    elif cfg.api_key:
        checks.append(_ok("api_key", "AGENTLOOP_API_KEY is configured"))
    else:
        checks.append(_ok("api_key", "not configured; not required for local-only use"))

    if cfg.auto_upload:
        checks.append(_ok("auto_upload", f"enabled -> {cfg.api_url}"))
    else:
        checks.append(_ok("auto_upload", "disabled; traces stay local"))

    if check_api:
        if explicit_api_url or cfg.auto_upload:
            checks.append(_health_check(cfg.api_url))
        else:
            checks.append(_ok("api_health", "skipped; no hosted API is configured"))

    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    return {
        "ok": failed == 0,
        "failed": failed,
        "warnings": warned,
        "checks": [check.to_dict() for check in checks],
    }


def run_production_check(
    *,
    check_api: bool = True,
    check_store: bool = True,
    allow_http: bool = False,
) -> dict[str, Any]:
    """Run strict checks for an internet-facing AgentLoop API/dashboard deployment."""

    checks: list[DoctorCheck] = []

    py_version = sys.version_info
    if py_version >= (3, 11):
        checks.append(_ok("python", f"Python {platform.python_version()}"))
    else:
        checks.append(
            _fail(
                "python",
                f"Python {platform.python_version()} is too old",
                "Use Python 3.11 or newer.",
            )
        )

    backend = os.getenv("AGENTLOOP_STORE_BACKEND", "sqlite").lower()
    if backend == "postgres":
        checks.append(_ok("store_backend", "postgres"))
    else:
        checks.append(
            _fail(
                "store_backend",
                f"{backend} is not production-ready",
                "Set AGENTLOOP_STORE_BACKEND=postgres.",
            )
        )

    database_source = postgres_connection_source()
    if database_source:
        checks.append(_ok("database_url", f"configured via {database_source}"))
    else:
        checks.append(
            _fail(
                "database_url",
                "missing",
                "Set AGENTLOOP_DATABASE_URL/DATABASE_URL or libpq PGHOST/PGSERVICE variables.",
            )
        )

    if check_store:
        try:
            db = get_store()
            db.init()
            checks.append(_ok("store", f"{db.__class__.__name__} initialized"))
        except Exception as exc:
            checks.append(
                _fail(
                    "store",
                    f"store initialization failed: {exc}",
                    "Check database credentials and network access.",
                )
            )

    if _env_enabled("AGENTLOOP_REQUIRE_API_KEY"):
        checks.append(_ok("api_auth", "AGENTLOOP_REQUIRE_API_KEY=true"))
    else:
        checks.append(
            _fail("api_auth", "API key auth is disabled", "Set AGENTLOOP_REQUIRE_API_KEY=true.")
        )

    admin_key = os.getenv("AGENTLOOP_ADMIN_API_KEY", "")
    weak_admin_values = {
        "",
        "change-me-admin-key",
        "replace-with-a-long-random-admin-secret",
        "dev-secret",
        "admin-secret",
    }
    if admin_key in weak_admin_values or len(admin_key) < 32:
        checks.append(
            _fail(
                "admin_api_key",
                "missing or too weak",
                "Set AGENTLOOP_ADMIN_API_KEY to a generated secret with at least 32 characters.",
            )
        )
    else:
        checks.append(_ok("admin_api_key", "configured"))

    cors_origins = [
        origin.strip()
        for origin in os.getenv("AGENTLOOP_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if not cors_origins:
        checks.append(
            _fail("cors_origins", "missing", "Set AGENTLOOP_CORS_ORIGINS to the dashboard origin.")
        )
    elif "*" in cors_origins:
        checks.append(
            _fail(
                "cors_origins",
                "wildcard origin is not allowed for production",
                "Use exact HTTPS origins.",
            )
        )
    else:
        insecure = [
            origin
            for origin in cors_origins
            if not origin.startswith("https://") and not _is_local_origin(origin)
        ]
        if insecure and not allow_http:
            checks.append(
                _fail(
                    "cors_origins",
                    f"non-HTTPS origins: {', '.join(insecure)}",
                    "Serve the dashboard over HTTPS and list its https:// origin.",
                )
            )
        elif insecure:
            checks.append(
                _warn(
                    "cors_origins",
                    f"non-HTTPS origins: {', '.join(insecure)}",
                    "Use HTTPS origins for production traffic.",
                )
            )
        else:
            checks.append(_ok("cors_origins", ", ".join(cors_origins)))

    if os.getenv("AGENTLOOP_API_KEY"):
        checks.append(
            _warn(
                "shared_api_key",
                "AGENTLOOP_API_KEY grants access to project 'default'; it cannot be revoked, "
                "listed or attributed in the audit log",
                "Unset AGENTLOOP_API_KEY on the server and give each team a project key "
                "created with the admin key.",
            )
        )

    checks.extend(_hosting_policy_checks())

    api_url = os.getenv("AGENTLOOP_API_URL", "")
    if not api_url:
        checks.append(_fail("api_url", "missing", "Set AGENTLOOP_API_URL to the public API URL."))
    elif api_url.startswith("https://"):
        checks.append(_ok("api_url", api_url))
    elif allow_http:
        checks.append(
            _warn("api_url", f"{api_url} is not HTTPS", "Use HTTPS for production traffic.")
        )
    else:
        checks.append(_fail("api_url", f"{api_url} is not HTTPS", "Use an HTTPS public API URL."))

    if check_api and api_url:
        checks.append(_health_check(api_url))
        checks.append(_ready_check(api_url))

    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    return {
        "ok": failed == 0,
        "failed": failed,
        "warnings": warned,
        "checks": [check.to_dict() for check in checks],
    }
