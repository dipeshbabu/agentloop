"""Contract for the CLI's commands and options while cli.py is split (#407).

The snapshot in tests/fixtures/cli_surface.json records every command path, in
help order, with its options, defaults and help text. Moving command code to
other modules must not change it. Regenerate deliberately, after reviewing a
real CLI change, with AGENTLOOP_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePath

import typer
import typer.main

from agentloop.entrypoint import app

SNAPSHOT = Path(__file__).parent / "fixtures" / "cli_surface.json"


def _param(param) -> dict:
    default = param.default
    if callable(default):
        return _row(param, repr("<callable>"))
    return _row(param, _portable(default))


def _portable(value) -> str:
    """repr() with paths written the same way on every platform.

    WindowsPath and PosixPath repr differently, so record Path('a/b') instead,
    including inside lists and tuples.
    """
    if isinstance(value, PurePath):
        return f"Path({value.as_posix()!r})"
    if isinstance(value, list):
        return "[" + ", ".join(_portable(item) for item in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(_portable(item) for item in value)
        return "(" + inner + ("," if len(value) == 1 else "") + ")"
    return repr(value)


def _row(param, default: str) -> dict:
    return {
        "name": param.name,
        "kind": type(param).__name__,
        "opts": list(param.opts),
        "secondary_opts": list(getattr(param, "secondary_opts", [])),
        "required": param.required,
        "default": default,
        "multiple": param.multiple,
        "is_flag": bool(getattr(param, "is_flag", False)),
        "hidden": bool(getattr(param, "hidden", False)),
        "type": getattr(param.type, "name", type(param.type).__name__),
    }


def _walk(command, path: list[str]) -> list[dict]:
    rows = [
        {
            "path": " ".join(path) or "<root>",
            "hidden": command.hidden,
            "deprecated": bool(command.deprecated),
            "help": (command.help or "").strip(),
            "params": [_param(p) for p in command.params],
        }
    ]
    # Typer may bundle its own Click, so check for a group by shape. Go through
    # list_commands/get_command, as --help and dispatch do: some groups load
    # only on lookup, so they are not in .commands until then.
    if hasattr(command, "list_commands"):
        context = typer.Context(command)
        for name in command.list_commands(context):
            rows.extend(_walk(command.get_command(context, name), [*path, name]))
    return rows


def surface() -> list[dict]:
    return _walk(typer.main.get_command(app), [])


def test_cli_surface_is_unchanged():
    current = surface()
    if os.environ.get("AGENTLOOP_UPDATE_GOLDEN") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert [row["path"] for row in current] == [row["path"] for row in expected]
    for now, before in zip(current, expected, strict=True):
        assert now == before, now["path"]


def test_shared_helpers_are_still_importable_from_cli():
    from agentloop import cli

    for name in (
        "app",
        "console",
        "_build_quality_report_from_file",
        "_load_trace",
        "_write_json",
        "AgentLoopClient",
    ):
        assert hasattr(cli, name), name
