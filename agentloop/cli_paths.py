"""Validate report destinations before a CLI command writes evidence artifacts."""

from __future__ import annotations

from pathlib import Path

import typer


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and left.samefile(right)


def validate_output_paths(*, sources: list[Path | None], outputs: dict[str, Path | None]) -> None:
    """Reject destinations aliasing source evidence or another output file."""
    selected = [(option, path) for option, path in outputs.items() if path is not None]
    for index, (option, path) in enumerate(selected):
        try:
            if path.exists() and not path.is_file():
                raise typer.BadParameter("Output path must name a regular file.", param_hint=option)
            parent = path.parent
            while not parent.exists():
                parent = parent.parent
            if not parent.is_dir():
                raise typer.BadParameter("Output parent must be a directory.", param_hint=option)
            source_collision = any(
                _same_file(path, source) for source in sources if source is not None
            )
            resolved = path.resolve()
            output_collision = any(
                _same_file(path, other)
                or resolved in other.resolve().parents
                or other.resolve() in resolved.parents
                for _, other in selected[:index]
            )
        except (OSError, RuntimeError) as exc:
            raise typer.BadParameter(
                "Cannot verify output paths; check their permissions and links.", param_hint=option
            ) from exc
        if source_collision:
            raise typer.BadParameter(
                "Output path must differ from all source evidence.", param_hint=option
            )
        if output_collision:
            raise typer.BadParameter("Output paths must differ from each other.", param_hint=option)
