# Coding agents (MCP)

`agentloop mcp` runs a local [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio. A coding agent that supports MCP can use it to inspect your
traces, findings, source locations and candidate changes. It uses the same APIs
as the CLI, and adds no dependencies.

```bash
agentloop mcp --root .
```

These are examples for two agents that support MCP. They aren't official
integrations with either product.

=== "Claude Code"

    ```bash
    claude mcp add agentloop -- agentloop mcp --root .
    ```

    Or commit a `.mcp.json` at the repository root:

    ```json
    {
      "mcpServers": {
        "agentloop": {"command": "agentloop", "args": ["mcp", "--root", "."]}
      }
    }
    ```

=== "Codex"

    In `~/.codex/config.toml`:

    ```toml
    [mcp_servers.agentloop]
    command = "agentloop"
    args = ["mcp", "--root", "/path/to/repo"]
    ```

An agent skill that explains how to use the tools is in
[`examples/agent_skills/agentloop/SKILL.md`](../examples/agent_skills/agentloop/SKILL.md).
Copy it into your agent's skills directory.

## Tools

| Tool | What it returns | Writes anything |
| --- | --- | --- |
| `list_traces` | JSON files under the root, or a subdirectory | No |
| `trace_summary` | Runtime, operation counts and models | No |
| `findings` | Ranked findings with their evidence | No |
| `patch_plan` | The dry-run [patch plan](SOURCE_PROVENANCE.md#how-patch-plans-use-it), with source locations | No |
| `execution_ir` | The [execution IR](EXECUTION_IR.md) | No |
| `timing_sensitivity` | [Simulated](TIMING_SENSITIVITY.md) critical-path gains | No |
| `propose_transformation` | A dry-run [transformation](TRANSFORMATIONS.md) candidate with its diff | No |
| `execute_candidate` | The candidate validated in a temporary git worktree | Only a temporary worktree, which is then removed. Off by default. |

`execute_candidate` exists only when the server is started with
`--allow-execution`, and each call needs `confirm: true`. It re-proposes the
change against `HEAD`, runs your validation command (an argv list, no shell),
and reports accepted or rejected. Your checkout itself is never edited.

## Safety

- **Confined paths.** Every path is relative to `--root`. Absolute paths, `..`,
  and symlinks that lead outside the root are refused.
- **Redacted text.** Model inputs and outputs recorded in traces are replaced
  with `[redacted]` unless you pass `--include-text`.
- **Untrusted content.** Results wrap their data with an `untrusted_content`
  notice. A trace can contain anything the application saw, including text
  written to manipulate an agent. The server never acts on trace content, and
  no trace content can enable execution.
- **Time limits.** Each tool call stops after `--timeout` seconds (default 60).
  A client can cancel a running request with `notifications/cancelled`. Tool
  calls run alongside the message reader, so the cancellation is seen while the
  call is still running. A cancelled request gets no response, as the MCP
  specification asks. Tool failures are reported as tool errors without
  internal detail.

## Protocol

The server speaks JSON-RPC 2.0, one message per line on stdin and stdout. It
supports these protocol versions: `2025-06-18`, `2025-03-26` and `2024-11-05`.
It implements `initialize`, `ping`, `tools/list` and `tools/call`, plus the
`notifications/initialized` and `notifications/cancelled` notifications. Tool
results carry both a text block and `structuredContent`.
