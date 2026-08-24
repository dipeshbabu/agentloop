from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from agentloop.version import __version__

ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = "agentloop-profiler"


def test_distribution_lock_import_and_cli_metadata_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    editable = next(
        package for package in lock["package"] if package.get("source") == {"editable": "."}
    )

    assert project["project"]["name"] == DIST_NAME
    assert project["project"]["version"] == __version__
    assert project["project"]["scripts"]["agentloop"] == "agentloop.entrypoint:app"
    assert editable["name"] == DIST_NAME
    assert editable["version"] == __version__


def test_install_and_release_docs_use_official_distribution() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    releasing = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")

    assert "python -m pip install agentloop-profiler" in readme
    assert "import agentloop" in readme
    assert "https://pypi.org/project/agentloop-profiler/" in readme
    assert "python -m pip install agentloop-profiler==X.Y.Z" in releasing
    assert "python -m pip install agentloop\n" not in readme
    assert "unrelated project" not in readme
    assert "unrelated package" not in releasing
