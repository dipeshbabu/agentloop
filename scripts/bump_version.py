"""Bump the package version and roll CHANGELOG.md's Unreleased section into a
dated release section.

Usage:
    python scripts/bump_version.py major|minor|patch
    python scripts/bump_version.py --set X.Y.Z

Updates, in lockstep: `pyproject.toml`'s `project.version`,
`agentloop/version.py`'s `__version__`, and `CHANGELOG.md` (renames
`## [Unreleased]` to a dated `## [x.y.z]` heading, inserts a fresh empty
`## [Unreleased]` above it, and appends the compare-link footer entries).
Refuses to run if `## [Unreleased]` has no content, so there is nothing
accidental to release.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess  # nosec B404 - only used below to run `uv lock` with a fixed argv, no shell
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
VERSION_PY = ROOT / "agentloop" / "version.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/dipeshbabu/agentloop"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")([^"]+)(")$', re.MULTILINE)
_VERSION_PY_VERSION = re.compile(r'^(__version__\s*=\s*")([^"]+)(")$', re.MULTILINE)
_UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\]\n", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^## \[", re.MULTILINE)
_COMPARE_LINK = re.compile(r"^\[Unreleased\]: .+$", re.MULTILINE)


class BumpError(RuntimeError):
    pass


def current_version() -> str:
    match = _VERSION_PY_VERSION.search(VERSION_PY.read_text(encoding="utf-8"))
    if not match:
        raise BumpError(f"could not find __version__ in {VERSION_PY}")
    return match.group(2)


def parse_semver(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match(version)
    if not match:
        raise BumpError(f"{version!r} is not a valid X.Y.Z semantic version")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compute_next_version(current: str, bump_type: str) -> str:
    major, minor, patch = parse_semver(current)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    if bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise BumpError(f"unknown bump type {bump_type!r}")


def update_pyproject(new_version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated, count = _PYPROJECT_VERSION.subn(rf"\g<1>{new_version}\g<3>", text, count=1)
    if count != 1:
        raise BumpError(f"could not find a version field to update in {PYPROJECT}")
    PYPROJECT.write_text(updated, encoding="utf-8")


def update_version_py(new_version: str) -> None:
    text = VERSION_PY.read_text(encoding="utf-8")
    updated, count = _VERSION_PY_VERSION.subn(rf"\g<1>{new_version}\g<3>", text, count=1)
    if count != 1:
        raise BumpError(f"could not find __version__ to update in {VERSION_PY}")
    VERSION_PY.write_text(updated, encoding="utf-8")


def _unreleased_section_body(changelog: str) -> str:
    heading_match = _UNRELEASED_HEADING.search(changelog)
    if not heading_match:
        raise BumpError(f"{CHANGELOG} has no '## [Unreleased]' heading")
    start = heading_match.end()
    next_heading = _NEXT_HEADING.search(changelog, pos=start)
    end = next_heading.start() if next_heading else len(changelog)
    return changelog[start:end]


def update_changelog(previous_version: str, new_version: str, *, today: str | None = None) -> None:
    changelog = CHANGELOG.read_text(encoding="utf-8")
    body = _unreleased_section_body(changelog)
    if not body.strip():
        raise BumpError(
            "CHANGELOG.md's '## [Unreleased]' section is empty; add entries for the "
            "changes being released before bumping the version."
        )

    date = today or datetime.date.today().isoformat()
    dated_heading = f"## [{new_version}] - {date}\n"
    changelog = _UNRELEASED_HEADING.sub(f"## [Unreleased]\n\n{dated_heading}", changelog, count=1)

    compare_link = f"[Unreleased]: {REPO_URL}/compare/v{new_version}...HEAD"
    new_version_link = f"[{new_version}]: {REPO_URL}/compare/v{previous_version}...v{new_version}"
    if _COMPARE_LINK.search(changelog):
        changelog = _COMPARE_LINK.sub(f"{compare_link}\n{new_version_link}", changelog, count=1)
    else:
        changelog = changelog.rstrip("\n") + f"\n\n{compare_link}\n{new_version_link}\n"

    CHANGELOG.write_text(changelog, encoding="utf-8")


def refresh_lockfile() -> None:
    # Fixed argv, no shell; "uv" is resolved via PATH like every other
    # `uv run`/`uv sync` invocation this project's tooling already relies on.
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)  # noqa: S603, S607  # nosec B603 B607


def bump(bump_type: str | None, explicit_version: str | None) -> str:
    previous = current_version()
    if explicit_version:
        parse_semver(explicit_version)
        new_version = explicit_version
    else:
        new_version = compute_next_version(previous, bump_type)  # type: ignore[arg-type]

    if parse_semver(new_version) <= parse_semver(previous):
        raise BumpError(f"new version {new_version} must be greater than current {previous}")

    update_changelog(previous, new_version)
    update_pyproject(new_version)
    update_version_py(new_version)
    refresh_lockfile()
    return new_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("bump_type", nargs="?", choices=["major", "minor", "patch"], default=None)
    group.add_argument("--set", dest="explicit_version", metavar="X.Y.Z")
    args = parser.parse_args(argv)

    try:
        new_version = bump(args.bump_type, args.explicit_version)
    except BumpError as exc:
        print(f"bump_version: {exc}", file=sys.stderr)
        return 1

    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
