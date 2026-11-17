# OpenAI Codex CLI

AgentLoop can profile and compare [Codex CLI](https://github.com/openai/codex)
runs through the guided workflow. The example
[runner](https://github.com/dipeshbabu/agentloop/blob/main/examples/codex_exec_runner.py)
runs `codex exec --json` for each task and converts its event stream into a
native AgentLoop trace. Codex is not modified and no plugin is installed.

Each task attempt is a real Codex run on your ChatGPT plan or API key and counts
against that usage.

## How events become a trace

The runner starts `codex exec --json --ephemeral --skip-git-repo-check
--sandbox read-only --cd DIR -` and sends the task prompt on stdin. Codex events
carry no timestamps, so the runner timestamps each JSONL line as it arrives.

| Codex event | AgentLoop event |
| --- | --- |
| `command_execution`, `file_change`, `mcp_tool_call`, `web_search` items | `tool_call` spanning `item.started` to `item.completed`; non-zero exit codes are errors |
| `agent_message` and `reasoning` items | `model_call` ending when the item arrived |
| Time before a tool call with no visible item | `model_call` named `codex_tool_decision` |
| `turn.completed` usage | Provider token counts on the turn's last model span, with `usage_scope: turn_total`, cached and reasoning tokens in metadata |
| `turn.failed` or `error` | Error on the root `codex_exec` span |

Codex reports usage once per turn, so per-span token counts other than the turn
total are recorded as unavailable instead of being estimated. The final agent
message is the answer scored by the frozen task checks.

## Run the guided workflow

Tasks use a prompt string or an object with `prompt`. The checked-in
[task set](https://github.com/dipeshbabu/agentloop/blob/main/examples/codex_repository_tasks.json)
asks three metadata questions about this repository with exact expected values.

```bash
agentloop improve capture --tasks examples/codex_repository_tasks.json --out runs/codex/baseline --timeout 600 -- python examples/codex_exec_runner.py --cd .
agentloop improve start --tasks examples/codex_repository_tasks.json --baseline runs/codex/baseline/capture.json --out runs/codex/session
```

Arguments after a second `--` go to `codex exec` unchanged. A candidate can
therefore change the model, reasoning effort, profile or features without
editing the runner. Capture it with the unchanged runner as an
[interleaved control](GUIDED_OPTIMIZATION.md#compare-against-an-interleaved-control):

```bash
agentloop improve capture --tasks examples/codex_repository_tasks.json --out runs/codex/paired --timeout 600 --control-arg python --control-arg examples/codex_exec_runner.py --control-arg --cd --control-arg . -- python examples/codex_exec_runner.py --cd . -- -c model_reasoning_effort="low"
agentloop improve finish --session runs/codex/session/session.json --candidate runs/codex/paired/capture.json --change "Lower reasoning effort for lookups" --out runs/codex/results
```

Runner options: `--cd` sets the directory Codex reads, `--instructions FILE`
prepends shared instructions to each prompt, `--model` passes `--model` and
records the label, `--condition` labels the trace, and `--codex` selects the
executable.

## First observed run

On 2026-11-14 the maintainer ran this task set with Codex CLI 0.167.1 on
Windows, using the configured model at its configured reasoning effort. Each of
the three tasks ran twice per condition.

- **Baseline:** all six answers were correct. Attempts took 15.6–29.8 seconds
  and used 38,755–39,901 input tokens to read one field. AgentLoop reported
  `split_large_step` on every attempt, pointing at the size of the model
  context.
- **Candidate:** disabled the unused `apps`, `browser_use` and `computer_use`
  features to shrink the tool context. It ran against an interleaved control,
  with order alternating between attempts.
- **Outcome:** all twelve answers were correct, and four of six pairs passed.
  Input tokens fell by about 2,600 (about 7%) in pairs where both runs took the
  same path. Both `engines.node` pairs were slower than their controls, by 12%
  and 19%, and failed the latency gates. **The change was rejected.**

Three runs, two controls and one candidate, used about 18,000–20,400 more input
tokens than their other repetition. Each of them took an extra model step. Codex
ran one tool, called the model again, and then ran another tool, instead of
running both tools in one step, and each model step resent about 19,000 tokens
of context. Several runs also read a user-level instruction file referenced by
the global Codex instructions. That read cost nothing extra when Codex ran it in
the same step as the requested file read. The extra step, not the read itself,
accounted for the increase. This path variation is larger than the measured
context saving.

The `repetition_detour` finding, available from AgentLoop 0.2.3, reports these
attempts automatically. On these 18 traces it flags exactly the three runs with
an extra model step and none of the others. More repetitions would be needed before
attributing a latency change to this configuration. These six pairs describe
one machine, one model configuration and three lookup tasks.
