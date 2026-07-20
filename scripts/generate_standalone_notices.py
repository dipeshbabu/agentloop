"""Generate complete license notices for a standalone AgentLoop build."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

LICENSE_FILENAME = re.compile(
    r"^(?:licen[cs]e|copying|notice|authors?|copyright)(?:[._-].*)?$",
    re.IGNORECASE,
)


class NoticeGenerationError(RuntimeError):
    """Raised when a complete notice bundle cannot be generated."""


class DistributionLike(Protocol):
    """Subset of importlib.metadata.Distribution used by the generator."""

    metadata: metadata.PackageMetadata
    version: str
    files: Sequence[object] | None

    def locate_file(self, path: object) -> Path: ...


@dataclass(frozen=True)
class DistributionNotice:
    """License material collected for one installed distribution."""

    name: str
    version: str
    declared_license: str
    license_files: tuple[tuple[str, str], ...]


def _python_license_path() -> Path:
    roots = {
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(sys.executable).resolve().parent,
        Path(sys.executable).resolve().parent.parent,
    }
    version_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for root in sorted(roots, key=str):
        candidates = (
            root / "LICENSE.txt",
            root / "LICENSE",
            root / "COPYING",
            root / "share" / "doc" / version_dir / "LICENSE.txt",
            root / "share" / "doc" / version_dir / "LICENSE",
            root / "share" / "doc" / version_dir / "copyright",
            root / "lib" / version_dir / "LICENSE.txt",
            root / "lib" / version_dir / "LICENSE",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    searched = ", ".join(str(root) for root in sorted(roots, key=str))
    raise NoticeGenerationError(f"CPython license file not found under: {searched}")


def _is_license_file(path: object, declared: set[str]) -> bool:
    normalized = str(path).replace("\\", "/").lstrip("./")
    basename = normalized.rsplit("/", 1)[-1]
    if LICENSE_FILENAME.match(basename):
        return True
    return any(
        normalized == item
        or normalized.endswith(f"/licenses/{item}")
        or normalized.endswith(f"/{item}")
        for item in declared
    )


def _distribution_notice(distribution: DistributionLike) -> DistributionNotice:
    name = str(distribution.metadata.get("Name") or "").strip()
    version = str(distribution.version or "").strip()
    if not name or not version:
        raise NoticeGenerationError("Installed distribution is missing its name or version")

    declared_files = {
        str(item).replace("\\", "/").lstrip("./")
        for item in (distribution.metadata.get_all("License-File") or [])
        if str(item).strip()
    }
    selected = sorted(
        {file for file in distribution.files or () if _is_license_file(file, declared_files)},
        key=lambda item: str(item).casefold(),
    )
    if not selected:
        raise NoticeGenerationError(f"No license or attribution file found for {name}=={version}")

    license_files: list[tuple[str, str]] = []
    for relative_path in selected:
        located = Path(distribution.locate_file(relative_path))
        try:
            content = located.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            raise NoticeGenerationError(
                f"Could not read {relative_path} for {name}=={version}: {exc}"
            ) from exc
        if not content:
            raise NoticeGenerationError(
                f"License or attribution file {relative_path} is empty for {name}=={version}"
            )
        license_files.append((str(relative_path).replace("\\", "/"), content))

    declared_license = str(
        distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or "Not declared in package metadata"
    ).strip()
    return DistributionNotice(name, version, declared_license, tuple(license_files))


def collect_distribution_notices(
    distributions: Iterable[DistributionLike],
) -> tuple[DistributionNotice, ...]:
    """Collect deterministic notices, rejecting incomplete package metadata."""

    notices = sorted(
        (_distribution_notice(distribution) for distribution in distributions),
        key=lambda notice: (notice.name.casefold(), notice.version),
    )
    seen: set[str] = set()
    for notice in notices:
        normalized_name = re.sub(r"[-_.]+", "-", notice.name).casefold()
        if normalized_name in seen:
            raise NoticeGenerationError(
                f"Duplicate installed distribution metadata found for {notice.name}"
            )
        seen.add(normalized_name)
    return tuple(notices)


def render_notices(
    *,
    target: str,
    python_version: str,
    python_license: str,
    distributions: Sequence[DistributionNotice],
) -> str:
    """Render a deterministic notice document for one platform target."""

    sections = [
        "AgentLoop standalone third-party notices",
        "========================================",
        "",
        f"Target: {target}",
        f"CPython: {python_version}",
        "",
        "This file was generated from the exact locked environment used to build",
        "the standalone executable. It includes the license and attribution files",
        "shipped by every installed component in that environment.",
        "",
        f"CPython {python_version}",
        "-" * (8 + len(python_version)),
        python_license.strip(),
    ]
    for notice in distributions:
        heading = f"{notice.name} {notice.version}"
        sections.extend(
            ["", heading, "-" * len(heading), f"Declared license: {notice.declared_license}"]
        )
        for relative_path, content in notice.license_files:
            sections.extend(["", f"Source file: {relative_path}", "", content])
    return "\n".join(sections).rstrip() + "\n"


def generate_notice_file(target: str, output: Path) -> None:
    python_license_path = _python_license_path()
    try:
        python_license = python_license_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise NoticeGenerationError(f"Could not read CPython license: {exc}") from exc
    if not python_license:
        raise NoticeGenerationError(f"CPython license file is empty: {python_license_path}")

    document = render_notices(
        target=target,
        python_version=sys.version.split()[0],
        python_license=python_license,
        distributions=collect_distribution_notices(metadata.distributions()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Release target name")
    parser.add_argument("--output", required=True, type=Path, help="Notice output path")
    args = parser.parse_args()
    try:
        generate_notice_file(args.target, args.output)
    except NoticeGenerationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
