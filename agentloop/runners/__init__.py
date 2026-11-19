"""Command-line runners that turn coding-agent runs into native AgentLoop traces.

Each runner reads one ``agentloop improve capture`` request on stdin, runs the
agent CLI and writes a trace to the requested path:

- ``python -m agentloop.runners.codex`` runs OpenAI Codex CLI (``codex exec``);
- ``python -m agentloop.runners.claude_code`` runs Claude Code (``claude -p``).
"""
