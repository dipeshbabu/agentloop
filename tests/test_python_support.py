from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_python_support_is_consistent_across_metadata_and_ci() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    match = re.search(r"^FROM python:([0-9]+\.[0-9]+)-slim$", dockerfile, re.MULTILINE)
    assert match is not None
    container_python = match.group(1)
    matrix_line = next(
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("python-version: [")
    )

    assert f'"Programming Language :: Python :: {container_python}"' in pyproject
    assert f'"{container_python}"' in matrix_line
    assert 'expected_python="$(' in workflow
    assert 'actual_python="$(' in workflow
    assert 'test "$actual_python" = "$expected_python"' in workflow
    assert "sys.version_info[:2] ==" not in workflow
