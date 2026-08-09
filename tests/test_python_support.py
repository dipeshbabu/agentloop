from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_314_support_is_consistent_across_metadata_ci_and_container() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '"Programming Language :: Python :: 3.14"' in pyproject
    assert 'python-version: ["3.10", "3.13", "3.14"]' in workflow
    assert "FROM python:3.14-slim" in dockerfile
    assert "sys.version_info[:2] == (3, 14)" in workflow
