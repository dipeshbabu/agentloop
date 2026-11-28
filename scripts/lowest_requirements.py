"""Print a wheel's required dependencies pinned to their lowest allowed version.

CI installs these pins next to the wheel, so the CLI is smoke-tested with the
oldest Typer and Rich we accept while everything they depend on (such as
click) resolves to its newest release. ``uv --resolution lowest-direct`` does
not do this for a wheel: the wheel's own requirements count as indirect.

    python scripts/lowest_requirements.py dist/agentloop_profiler-*.whl
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from email.parser import Parser

# name>=version with nothing else: the only form our core requirements use.
_FLOOR = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][0-9A-Za-z.]*)$")


def lowest_pins(wheel: str) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(name).decode("utf-8"))
    pins = []
    for requirement in metadata.get_all("Requires-Dist") or []:
        if ";" in requirement:
            continue  # extras and environment markers are not core requirements
        match = _FLOOR.match(requirement.strip())
        if match is None:
            raise SystemExit(f"cannot pin requirement without a plain floor: {requirement!r}")
        pins.append(f"{match.group(1)}=={match.group(2)}")
    if not pins:
        raise SystemExit("wheel declares no core requirements")
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel")
    args = parser.parse_args(argv)
    print("\n".join(lowest_pins(args.wheel)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
