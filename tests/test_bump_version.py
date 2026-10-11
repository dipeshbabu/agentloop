from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bump_version", Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
)
bump_version = importlib.util.module_from_spec(_SPEC)
sys.modules["bump_version"] = bump_version
_SPEC.loader.exec_module(bump_version)


CHANGELOG_TEMPLATE = """\
# Changelog

## [Unreleased]
{unreleased_body}
## [0.1.0] - 2026-07-18

### Added

- Something from the previous release.

[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.1.0
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    version_py = tmp_path / "version.py"
    changelog = tmp_path / "CHANGELOG.md"
    lockfile = tmp_path / "uv.lock"

    pyproject.write_text('[project]\nname = "agentloop-profiler"\nversion = "0.1.0"\n')
    version_py.write_text('from __future__ import annotations\n\n__version__ = "0.1.0"\n')
    changelog.write_text(
        CHANGELOG_TEMPLATE.format(unreleased_body="\n### Added\n\n- A pending change.\n\n")
    )

    monkeypatch.setattr(bump_version, "PYPROJECT", pyproject)
    monkeypatch.setattr(bump_version, "VERSION_PY", version_py)
    monkeypatch.setattr(bump_version, "CHANGELOG", changelog)
    monkeypatch.setattr(bump_version, "LOCKFILE", lockfile)
    monkeypatch.setattr(bump_version, "refresh_lockfile", lambda: None)
    return {
        "pyproject": pyproject,
        "version_py": version_py,
        "changelog": changelog,
        "lockfile": lockfile,
    }


# --- pure version math -----------------------------------------------------


@pytest.mark.parametrize(
    ("current", "bump_type", "expected"),
    [
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.1", "patch", "0.1.2"),
        ("0.1.0", "minor", "0.2.0"),
        ("0.1.0", "major", "1.0.0"),
        ("1.9.9", "patch", "1.9.10"),
        ("0.0.1", "major", "1.0.0"),
    ],
)
def test_compute_next_version(current, bump_type, expected):
    assert bump_version.compute_next_version(current, bump_type) == expected


def test_parse_semver_rejects_non_semver_strings():
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("v1.2.3")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("1.2")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("1.2.3-rc1")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("0.01.0")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("0.1.01")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("0.1.٢")
    with pytest.raises(bump_version.BumpError):
        bump_version.parse_semver("0.1.1\n")


# --- end-to-end bump() -------------------------------------------------------


def test_bump_updates_all_three_files_in_lockstep(repo):
    new_version = bump_version.bump("minor", None)

    assert new_version == "0.2.0"
    assert 'version = "0.2.0"' in repo["pyproject"].read_text()
    assert '__version__ = "0.2.0"' in repo["version_py"].read_text()


def test_bump_moves_unreleased_content_into_a_dated_section(repo):
    bump_version.bump("patch", None)

    changelog = repo["changelog"].read_text()
    assert "## [Unreleased]\n\n## [0.1.1] -" in changelog
    unreleased_section = changelog.split("## [Unreleased]")[1].split("## [0.1.1]")[0]
    assert unreleased_section.strip() == ""
    assert "### Added\n\n- A pending change." in changelog
    # the moved content must land inside the new dated section, before 0.1.0's
    dated_index = changelog.index("## [0.1.1]")
    pending_index = changelog.index("A pending change.")
    old_release_index = changelog.index("## [0.1.0]")
    assert dated_index < pending_index < old_release_index


def test_bump_updates_compare_link_footer(repo):
    bump_version.bump("patch", None)

    changelog = repo["changelog"].read_text()
    assert (
        "[Unreleased]: https://github.com/dipeshbabu/agentloop/compare/v0.1.1...HEAD" in changelog
    )
    assert "[0.1.1]: https://github.com/dipeshbabu/agentloop/compare/v0.1.0...v0.1.1" in changelog
    assert "[0.1.0]: https://github.com/dipeshbabu/agentloop/releases/tag/v0.1.0" in changelog


def test_bump_refuses_when_unreleased_section_is_empty(repo):
    repo["changelog"].write_text(CHANGELOG_TEMPLATE.format(unreleased_body="\n"))
    original_pyproject = repo["pyproject"].read_text()

    with pytest.raises(bump_version.BumpError, match="empty"):
        bump_version.bump("patch", None)

    # nothing should have been written
    assert repo["pyproject"].read_text() == original_pyproject
    assert '__version__ = "0.1.0"' in repo["version_py"].read_text()


def test_bump_rejects_explicit_version_not_greater_than_current(repo):
    with pytest.raises(bump_version.BumpError, match="must be greater"):
        bump_version.bump(None, "0.0.9")
    with pytest.raises(bump_version.BumpError, match="must be greater"):
        bump_version.bump(None, "0.1.0")

    assert '__version__ = "0.1.0"' in repo["version_py"].read_text()


def test_bump_accepts_explicit_version_override(repo):
    new_version = bump_version.bump(None, "2.0.0")

    assert new_version == "2.0.0"
    assert '__version__ = "2.0.0"' in repo["version_py"].read_text()


def test_bump_is_idempotent_on_failure_no_partial_writes(repo):
    original_pyproject = repo["pyproject"].read_text()
    original_version_py = repo["version_py"].read_text()
    original_changelog = repo["changelog"].read_text()

    with pytest.raises(bump_version.BumpError):
        bump_version.bump(None, "0.0.1")  # lower than current

    assert repo["pyproject"].read_text() == original_pyproject
    assert repo["version_py"].read_text() == original_version_py
    assert repo["changelog"].read_text() == original_changelog


# --- failed updates -------------------------------------------------------


@pytest.mark.parametrize("existing_lock", [False, True])
def test_failed_lock_refresh_restores_files_and_allows_retry(repo, monkeypatch, existing_lock):
    if existing_lock:
        repo["lockfile"].write_bytes(b"version = 1\r\n# original lock\r\n")
    originals = {path: path.read_bytes() if path.exists() else None for path in repo.values()}

    def failed_refresh():
        repo["lockfile"].write_text("partial lock")
        raise subprocess.CalledProcessError(1, ["uv", "lock"])

    monkeypatch.setattr(bump_version, "refresh_lockfile", failed_refresh)
    assert bump_version.main(["patch"]) == 1
    for path, original in originals.items():
        assert (path.read_bytes() if path.exists() else None) == original

    monkeypatch.setattr(bump_version, "refresh_lockfile", lambda: None)
    assert bump_version.bump("patch", None) == "0.1.1"
    assert repo["changelog"].read_text().count("## [0.1.1]") == 1


def test_missing_pyproject_version_does_not_roll_changelog_forward(repo):
    repo["pyproject"].write_text('[project]\nname = "agentloop-profiler"\n')
    originals = {path: path.read_bytes() for path in repo.values() if path.exists()}
    with pytest.raises(bump_version.BumpError, match="version field"):
        bump_version.bump("patch", None)
    assert {path: path.read_bytes() for path in originals} == originals


def test_interrupted_lock_refresh_restores_files(repo, monkeypatch):
    originals = {path: path.read_bytes() for path in repo.values() if path.exists()}

    def interrupted_refresh():
        repo["lockfile"].write_text("partial lock")
        raise KeyboardInterrupt

    monkeypatch.setattr(bump_version, "refresh_lockfile", interrupted_refresh)
    with pytest.raises(KeyboardInterrupt):
        bump_version.bump("patch", None)
    assert {path: path.read_bytes() for path in originals} == originals
    assert not repo["lockfile"].exists()


def test_failed_version_write_restores_prior_changes(repo, monkeypatch):
    originals = {path: path.read_bytes() for path in repo.values() if path.exists()}
    original_write = Path.write_text

    def failed_write(path, *args, **kwargs):
        if path == repo["version_py"]:
            path.write_bytes(b"partial version")
            raise OSError("disk write failed")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failed_write)
    with pytest.raises(bump_version.BumpError, match="previous files restored"):
        bump_version.bump("patch", None)
    assert {path: path.read_bytes() for path in originals} == originals


def test_failed_restore_reports_file_and_continues_restoring_others(repo, monkeypatch):
    original_version = repo["version_py"].read_bytes()
    original_write = Path.write_bytes

    def failed_refresh():
        raise FileNotFoundError("uv not installed")

    def failed_restore(path, *args, **kwargs):
        if path == repo["changelog"]:
            raise PermissionError("file locked")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(bump_version, "refresh_lockfile", failed_refresh)
    monkeypatch.setattr(Path, "write_bytes", failed_restore)
    with pytest.raises(bump_version.BumpError, match="could not restore") as error:
        bump_version.bump("patch", None)
    assert str(repo["changelog"]) in str(error.value)
    assert repo["version_py"].read_bytes() == original_version
    assert 'version = "0.1.0"' in repo["pyproject"].read_text()


# --- CLI -----------------------------------------------------------------


def test_main_requires_bump_type_or_set(repo):
    with pytest.raises(SystemExit):
        bump_version.main([])


def test_main_rejects_both_bump_type_and_set(repo):
    with pytest.raises(SystemExit):
        bump_version.main(["patch", "--set", "9.9.9"])


def test_main_prints_new_version_on_success(repo, capsys):
    exit_code = bump_version.main(["patch"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "0.1.1"
