# Claude Code

AgentLoop can profile and compare [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
runs through the guided workflow. The example
[runner](https://github.com/dipeshbabu/agentloop/blob/main/examples/claude_code_runner.py)
runs `claude -p --output-format stream-json --verbose` for each task and
converts its event stream into a native AgentLoop trace. Claude Code is not
modified and no plugin is installed.

Each task attempt is a real Claude Code run on your Claude plan or API key and
counts against that usage.

## How events become a trace

The runner starts Claude Code in the directory given by `--cd`, sends the task
prompt on stdin and pre-approves only the read-only `Read`, `Grep` and `Glob`
tools with `--allowedTools`. A request for any other tool is denied in
non-interactive mode and recorded as a failed tool call. Stream events carry no
timestamps, so the runner timestamps each JSON line as it arrives.

| Claude Code event | AgentLoop event |
| --- | --- |
| `assistant` events sharing one `message.id` | One `model_call` named `claude_response`, from the session start or the last tool result until the last part of that response arrived |
| `tool_use` block | `tool_call` named after the tool, ending when its `tool_result` arrives; `is_error` results are errors |
| `message.usage` | Provider token counts with `usage_scope: request`. Input tokens include cache reads and writes, which are also listed in metadata |
| `result` | The answer scored by the frozen task checks. Turns, durations and reported cost are kept on the root `claude_code` span |
| `result` with `is_error` or a non-success subtype | Error on the root span |

Claude Code may emit each content block of a response as its own `assistant`
event with repeated usage. The runner counts each response once.

## Run the guided workflow

Tasks use a prompt string or an object with `prompt`. The Codex
[task set](https://github.com/dipeshbabu/agentloop/blob/main/examples/codex_repository_tasks.json)
works unchanged: it asks three metadata questions about this repository with
exact expected values.

AgentLoop 0.2.5 and newer compare an unchanged and a changed configuration in
one command. Arguments after a second `--` go to `claude` unchanged, so the
candidate below only lowers the turn limit:

```bash
agentloop improve compare --tasks examples/codex_repository_tasks.json --out runs/claude --timeout 600 --change "Limit lookups to four turns" --before-arg python --before-arg examples/claude_code_runner.py --before-arg --cd --before-arg . -- python examples/claude_code_runner.py --cd . -- --max-turns 4
```

Runner options: `--cd` sets the directory Claude Code works in,
`--instructions FILE` prepends shared instructions to each prompt, `--model`
passes `--model` and records the label, `--condition` labels the trace, and
`--claude` selects the executable.

The conversion is tested against the documented stream shape and an end-to-end
run through a stand-in executable. A recorded run against a live Claude Code
installation has not yet been published here.
