from __future__ import annotations

from pathlib import Path

import pytest

from agentloop.autoinstrument import auto_instrument, detect_integrations
from agentloop.doctor import run_doctor, run_production_check
from agentloop.runtime import configure_from_env, reset_runtime


def _reload_runtime_from_env() -> None:
    reset_runtime()
    configure_from_env()


def test_run_doctor_without_api(tmp_path: Path, monkeypatch) -> None:
    for name in [
        "AGENTLOOP_API_URL",
        "AGENTLOOP_API_KEY",
        "AGENTLOOP_AUTO_UPLOAD",
        "AGENTLOOP_REQUIRE_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)
    _reload_runtime_from_env()

    result = run_doctor(check_api=True, runs_dir=tmp_path / "runs")

    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["warnings"] == 0
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["python"]["status"] == "ok"
    assert by_name["runs_dir"]["status"] == "ok"
    assert by_name["store"]["status"] == "ok"
    assert by_name["api_key"] == {
        "name": "api_key",
        "status": "ok",
        "detail": "not configured; not required for local-only use",
    }
    assert by_name["auto_upload"] == {
        "name": "auto_upload",
        "status": "ok",
        "detail": "disabled; traces stay local",
    }
    assert by_name["api_health"] == {
        "name": "api_health",
        "status": "ok",
        "detail": "skipped; no hosted API is configured",
    }
    dependency_names = {name for name in by_name if name.startswith("dependency:")}
    assert dependency_names == {"dependency:typer", "dependency:rich"}


def test_run_doctor_checks_explicit_api_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_API_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("AGENTLOOP_AUTO_UPLOAD", raising=False)
    _reload_runtime_from_env()

    result = run_doctor(check_api=True, runs_dir=tmp_path / "runs")

    api_health = next(check for check in result["checks"] if check["name"] == "api_health")
    assert api_health["status"] == "warn"
    assert "Could not reach" in api_health["detail"]


def test_run_doctor_warns_when_authenticated_auto_upload_has_no_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTLOOP_AUTO_UPLOAD", "true")
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.delenv("AGENTLOOP_API_KEY", raising=False)
    _reload_runtime_from_env()

    result = run_doctor(check_api=False, runs_dir=tmp_path / "runs")

    api_key = next(check for check in result["checks"] if check["name"] == "api_key")
    assert api_key["status"] == "warn"
    assert result["warnings"] == 1


def test_detect_integrations_returns_structured_result() -> None:
    result = detect_integrations()
    payload = result.to_dict()

    assert set(payload.keys()) == {"ok", "available", "unavailable"}
    assert isinstance(payload["available"], list)
    assert isinstance(payload["unavailable"], dict)
    # Vercel is JS-side and can never be reported as an available Python package.
    assert "vercel_ai_sdk" in payload["unavailable"]
    assert "vercel_ai_sdk" not in payload["available"]


def test_detect_integrations_is_idempotent() -> None:
    assert detect_integrations().to_dict() == detect_integrations().to_dict()


def test_detect_integrations_reports_missing_package(monkeypatch) -> None:
    import agentloop.autoinstrument as ai

    def fake_available(module_name: str) -> bool:
        return module_name == "openai"

    monkeypatch.setattr(ai, "_module_available", fake_available)
    payload = detect_integrations().to_dict()

    assert "openai" in payload["available"]
    assert payload["unavailable"]["langgraph"] == "package not installed"
    assert payload["unavailable"]["crewai"] == "package not installed"


def test_auto_instrument_is_deprecated_alias() -> None:
    with pytest.warns(DeprecationWarning):
        result = auto_instrument()

    # The deprecated alias returns the same detection contract.
    assert set(result.to_dict().keys()) == {"ok", "available", "unavailable"}


def test_production_check_rejects_unsafe_defaults(monkeypatch) -> None:
    for name in [
        "AGENTLOOP_STORE_BACKEND",
        "AGENTLOOP_DATABASE_URL",
        "AGENTLOOP_POSTGRES_PASSWORD_FILE",
        "DATABASE_URL",
        "PGHOST",
        "PGHOSTADDR",
        "PGSERVICE",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "AGENTLOOP_REQUIRE_API_KEY",
        "AGENTLOOP_ADMIN_API_KEY",
        "AGENTLOOP_CORS_ORIGINS",
        "AGENTLOOP_API_URL",
    ]:
        monkeypatch.delenv(name, raising=False)

    result = run_production_check(check_api=False, check_store=False)

    assert result["ok"] is False
    failed_names = {check["name"] for check in result["checks"] if check["status"] == "fail"}
    assert "store_backend" in failed_names
    assert "api_auth" in failed_names
    assert "admin_api_key" in failed_names
    assert "cors_origins" in failed_names
    assert "api_url" in failed_names


def test_production_check_accepts_safe_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "postgres")
    monkeypatch.setenv(
        "AGENTLOOP_DATABASE_URL", "postgresql://agentloop:secret@example.com:5432/agentloop"
    )
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "x" * 40)
    monkeypatch.setenv("AGENTLOOP_CORS_ORIGINS", "https://dashboard.example.com")
    monkeypatch.setenv("AGENTLOOP_API_URL", "https://api.example.com")

    result = run_production_check(check_api=False, check_store=False)

    assert result["ok"] is True
    assert result["failed"] == 0


def test_production_check_accepts_libpq_environment(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "postgres")
    monkeypatch.delenv("AGENTLOOP_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "agentloop")
    monkeypatch.setenv("PGUSER", "agentloop")
    monkeypatch.setenv("AGENTLOOP_POSTGRES_PASSWORD_FILE", "/run/secrets/postgres_password")
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "x" * 40)
    monkeypatch.setenv("AGENTLOOP_CORS_ORIGINS", "https://dashboard.example.com")
    monkeypatch.setenv("AGENTLOOP_API_URL", "https://api.example.com")

    result = run_production_check(check_api=False, check_store=False)

    assert result["ok"] is True
    database_check = next(check for check in result["checks"] if check["name"] == "database_url")
    assert database_check["detail"] == "configured via libpq environment"
