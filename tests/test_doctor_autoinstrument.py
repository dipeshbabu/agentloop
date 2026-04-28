from __future__ import annotations

from pathlib import Path

from agentloop.autoinstrument import auto_instrument
from agentloop.doctor import run_doctor


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
