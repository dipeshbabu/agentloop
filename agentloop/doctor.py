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
    url = api_url.rstrip("/") + "/health"
    try:
        request = Request(url, headers={"User-Agent": "agentloop-doctor"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-provided local/dev URL health check
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


def run_doctor(*, check_api: bool = True, runs_dir: Path | str = "runs") -> dict[str, Any]:
    """Run local setup checks for SDK, CLI, store, and hosted API wiring."""

    checks: list[DoctorCheck] = []

    py_version = sys.version_info
    if py_version >= (3, 10):
        checks.append(_ok("python", f"Python {platform.python_version()}"))
    else:
        checks.append(_fail("python", f"Python {platform.python_version()} is too old", "Use Python 3.10 or newer."))

    for module_name in ["typer", "rich", "pydantic"]:
        try:
            importlib.import_module(module_name)
            checks.append(_ok(f"dependency:{module_name}", "importable"))
        except ImportError:
            checks.append(_fail(f"dependency:{module_name}", "not installed", "Run: pip install -e '.[all,dev]'"))

    out_dir = Path(runs_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".agentloop_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(_ok("runs_dir", f"{out_dir} is writable"))
    except Exception as exc:
        checks.append(_fail("runs_dir", f"{out_dir} is not writable: {exc}", "Choose a writable path or set AGENTLOOP_EXPORT_DIR."))

    try:
        db = get_store()
        db.init()
        checks.append(_ok("store", f"{db.__class__.__name__} initialized"))
    except Exception as exc:
        checks.append(_fail("store", f"store initialization failed: {exc}", "Check AGENTLOOP_STORE_BACKEND and database credentials."))

    cfg = get_runtime_config()
    if cfg.auto_upload and not cfg.api_key and os.getenv("AGENTLOOP_REQUIRE_API_KEY", "").lower() in {"1", "true", "yes", "on"}:
        checks.append(_warn("api_key", "auto upload is enabled but AGENTLOOP_API_KEY is empty", "Create a key with: agentloop create-api-key --project-id demo"))
    elif cfg.api_key:
        checks.append(_ok("api_key", "AGENTLOOP_API_KEY is configured"))
    else:
        checks.append(_warn("api_key", "no API key configured; OK for local API when auth is disabled"))

    if cfg.auto_upload:
        checks.append(_ok("auto_upload", f"enabled -> {cfg.api_url}"))
    else:
        checks.append(_warn("auto_upload", "disabled", "Set AGENTLOOP_AUTO_UPLOAD=true to upload traces automatically."))

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
