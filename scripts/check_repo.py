from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "build", "dist"}
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"$', re.MULTILINE)
REQUIRED_COMMUNITY_FILES = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "LICENSE",
    "MAINTAINERS.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_LICENSES.md",
}


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
    )


def local_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]

    if not target or target.startswith("#"):
        return None

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None

    path_text = unquote(parsed.path)
    if not path_text:
        return None
    if path_text.startswith("/"):
        return ROOT / path_text.lstrip("/")
    return source.parent / path_text


def check_markdown_links() -> list[str]:
    errors: list[str] = []
    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            target = local_link_target(source, match.group(1))
            if target is not None and not target.resolve().exists():
                relative_source = source.relative_to(ROOT)
                errors.append(f"{relative_source}: missing local link target {match.group(1)!r}")
    return errors


def check_metadata() -> list[str]:
    errors: list[str] = []
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    version_text = (ROOT / "agentloop" / "version.py").read_text(encoding="utf-8")
    version_match = VERSION_PATTERN.search(version_text)
    package_version = version_match.group(1) if version_match else None
    if project.get("version") != package_version:
        errors.append(
            "pyproject.toml project.version does not match agentloop/version.py __version__"
        )

    if project.get("license") != "Apache-2.0":
        errors.append("pyproject.toml project.license must match the Apache-2.0 LICENSE")

    package_includes = metadata.get("tool", {}).get("setuptools", {}).get("packages", {})
    includes = package_includes.get("find", {}).get("include", [])
    for package in ("agentloop*", "dashboard*"):
        if package not in includes:
            errors.append(f"pyproject.toml package discovery is missing {package!r}")

    for relative_path in sorted(REQUIRED_COMMUNITY_FILES):
        if not (ROOT / relative_path).is_file():
            errors.append(f"missing required community file: {relative_path}")
    return errors


def main() -> int:
    errors = check_metadata() + check_markdown_links()
    if errors:
        for error in errors:
            print(f"repository check failed: {error}", file=sys.stderr)
        return 1
    print("Repository metadata and local Markdown links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
