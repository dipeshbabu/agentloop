"""Helpers shared by the coding-agent runners."""

from __future__ import annotations

import time

OUTPUT_LIMIT = 4096


def task_prompt(task_input, instructions):
    """Return the prompt for a task input, with optional shared instructions first."""
    if isinstance(task_input, dict):
        task_input = task_input.get("prompt") or task_input.get("question")
    if not isinstance(task_input, str) or not task_input.strip():
        raise ValueError("Task input must be a prompt string or an object with 'prompt'")
    return f"{instructions.strip()}\n\n{task_input}" if instructions else task_input


def clip(value):
    """Bound recorded text so traces stay small."""
    if not isinstance(value, str):
        return None
    return value if len(value) <= OUTPUT_LIMIT else value[:OUTPUT_LIMIT] + "\n[truncated]"


def read_stream(process, lines):
    """Timestamp each output line on arrival; agent events carry no timestamps."""
    for raw in process.stdout:
        lines.append((time.perf_counter(), raw))


def split_passthrough(argv):
    """Split runner options from arguments passed unchanged to the agent CLI."""
    split = argv.index("--") if "--" in argv else len(argv)
    return argv[:split], argv[split + 1 :]
