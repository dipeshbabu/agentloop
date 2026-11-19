"""Run OpenAI Codex CLI on guided tasks; kept for existing commands.

The runner ships in the package as ``python -m agentloop.runners.codex``.
This script forwards to it so commands written against a checkout keep working.
"""

from agentloop.runners.codex import convert, main, task_prompt

__all__ = ["convert", "main", "task_prompt"]

if __name__ == "__main__":
    main()
