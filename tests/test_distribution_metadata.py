from __future__ import annotations

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

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


@pytest.mark.parametrize(
    "extra,package,vulnerable",
    [
        ("dashboard", "pillow", "12.3.0"),
        ("dashboard", "certifi", "2026.6.17"),
        ("dashboard", "urllib3", "2.7.0"),
        ("instrumentation", "certifi", "2026.6.17"),
        ("all", "pillow", "12.3.0"),
        ("all", "certifi", "2026.6.17"),
        ("all", "urllib3", "2.7.0"),
    ],
)
def test_published_extras_reject_known_vulnerable_installed_versions(extra, package, vulnerable):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        Requirement(value) for value in project["project"]["optional-dependencies"][extra]
    ]
    applicable = [
        item
        for item in requirements
        if canonicalize_name(item.name) == package
        and (
            item.marker is None
            or item.marker.evaluate(
                {"python_version": "3.11", "python_full_version": "3.11.0", "extra": extra}
            )
        )
    ]
    assert applicable, f"{extra} needs a published security requirement for {package}"
    assert any(Version(vulnerable) not in item.specifier for item in applicable)


@pytest.mark.parametrize(
    "group,package,vulnerable",
    [
        ("dev", "filelock", "3.29.7"),
        ("dev", "virtualenv", "21.14.5"),
        ("dev", "certifi", "2026.6.17"),
        ("standalone", "setuptools", "83.0.0"),
    ],
)
def test_dependency_groups_reject_vulnerable_tools_without_uv_constraints(
    group, package, vulnerable
):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(value) for value in project["dependency-groups"][group]]
    applicable = [item for item in requirements if canonicalize_name(item.name) == package]
    assert applicable, f"{group} needs a direct security requirement for {package}"
    assert any(Version(vulnerable) not in item.specifier for item in applicable)
