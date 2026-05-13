from __future__ import annotations

from pathlib import Path

from agentloop.autoinstrument import auto_instrument
from agentloop.doctor import run_doctor, run_production_check


def test_run_doctor_without_api(tmp_path: Path) -> None:
    result = run_doctor(check_api=False, runs_dir=tmp_path / "runs")

    assert "ok" in result
    assert result["failed"] == 0
    assert any(check["name"] == "python" for check in result["checks"])
    assert any(check["name"] == "runs_dir" for check in result["checks"])
    assert any(check["name"] == "store" for check in result["checks"])


def test_auto_instrument_returns_structured_result() -> None:
    result = auto_instrument()
    payload = result.to_dict()

    assert set(payload.keys()) == {"ok", "enabled", "skipped"}
    assert isinstance(payload["enabled"], list)
    assert isinstance(payload["skipped"], dict)
    assert "vercel_ai_sdk" in payload["skipped"]


def test_production_check_rejects_unsafe_defaults(monkeypatch) -> None:
    for name in [
        "AGENTLOOP_STORE_BACKEND",
        "AGENTLOOP_DATABASE_URL",
        "DATABASE_URL",
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
    monkeypatch.setenv("AGENTLOOP_DATABASE_URL", "postgresql://agentloop:secret@example.com:5432/agentloop")
    monkeypatch.setenv("AGENTLOOP_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("AGENTLOOP_ADMIN_API_KEY", "x" * 40)
    monkeypatch.setenv("AGENTLOOP_CORS_ORIGINS", "https://dashboard.example.com")
    monkeypatch.setenv("AGENTLOOP_API_URL", "https://api.example.com")

    result = run_production_check(check_api=False, check_store=False)

    assert result["ok"] is True
    assert result["failed"] == 0
