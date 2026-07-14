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


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def run_doctor(*, check_api: bool = True, runs_dir: Path | str = "runs") -> dict[str, Any]:
    """Run local setup checks for SDK, CLI, store, and hosted API wiring."""

    checks: list[DoctorCheck] = []

    py_version = sys.version_info
    if py_version >= (3, 10):
        checks.append(_ok("python", f"Python {platform.python_version()}"))
    else:
        checks.append(
            _fail(
                "python",
                f"Python {platform.python_version()} is too old",
                "Use Python 3.10 or newer.",
            )
        )

    for module_name in ["typer", "rich", "pydantic"]:
        try:
            importlib.import_module(module_name)
            checks.append(_ok(f"dependency:{module_name}", "importable"))
        except ImportError:
            checks.append(
                _fail(
                    f"dependency:{module_name}",
                    "not installed",
                    "Run: uv sync --locked --all-extras --dev",
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
    if (
        cfg.auto_upload
        and not cfg.api_key
        and os.getenv("AGENTLOOP_REQUIRE_API_KEY", "").lower() in {"1", "true", "yes", "on"}
    ):
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
        checks.append(
            _warn("api_key", "no API key configured; OK for local API when auth is disabled")
        )

    if cfg.auto_upload:
        checks.append(_ok("auto_upload", f"enabled -> {cfg.api_url}"))
    else:
        checks.append(
            _warn(
                "auto_upload",
                "disabled",
                "Set AGENTLOOP_AUTO_UPLOAD=true to upload traces automatically.",
            )
        )

    if check_api:
        checks.append(_health_check(cfg.api_url))

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
    if py_version >= (3, 10):
        checks.append(_ok("python", f"Python {platform.python_version()}"))
    else:
        checks.append(
            _fail(
                "python",
                f"Python {platform.python_version()} is too old",
                "Use Python 3.10 or newer.",
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

    database_url = os.getenv("AGENTLOOP_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url:
        checks.append(_ok("database_url", "configured"))
    else:
        checks.append(
            _fail("database_url", "missing", "Set AGENTLOOP_DATABASE_URL or DATABASE_URL.")
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
        checks.append(_ok("cors_origins", ", ".join(cors_origins)))

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
