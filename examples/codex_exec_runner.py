"""Run OpenAI Codex CLI on guided tasks and record a native AgentLoop trace.

The runner receives the standard `agentloop improve capture` JSON request on
stdin. It runs `codex exec --json --ephemeral` in a read-only sandbox, timestamps
each JSONL event as it arrives, and converts the stream into AgentLoop events:

- each shell command, file change, MCP tool call or web search is a tool span;
- each agent message or reasoning item is a model span ending when it arrived;
- Codex reports token usage once per turn, so the turn total is attached to the
  last model span of that turn with ``usage_scope = "turn_total"``.

The final agent message is the task answer that the frozen checks score.
Arguments after ``--`` are passed to ``codex exec`` unchanged, so a candidate can
change the model, profile or configuration without editing this file.

Example:

    agentloop improve capture --tasks examples/codex_repository_tasks.json \
        --out runs/codex-baseline -- python examples/codex_exec_runner.py --cd .
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 - runs the operator's Codex CLI with explicit argv
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.tokens import PROVIDER, UNAVAILABLE
from agentloop.tracer import AgentTrace

TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}
MODEL_ITEMS = {"agent_message", "reasoning"}
OUTPUT_LIMIT = 4096


def task_prompt(task_input, instructions):
    if isinstance(task_input, dict):
        task_input = task_input.get("prompt") or task_input.get("question")
    if not isinstance(task_input, str) or not task_input.strip():
        raise ValueError("Task input must be a prompt string or an object with 'prompt'")
    return f"{instructions.strip()}\n\n{task_input}" if instructions else task_input


def clip(value):
    if not isinstance(value, str):
        return None
    return value if len(value) <= OUTPUT_LIMIT else value[:OUTPUT_LIMIT] + "\n[truncated]"


def read_stream(process, lines):
    """Timestamp each JSONL line on arrival; Codex events carry no timestamps."""
    for raw in process.stdout:
        lines.append((time.perf_counter(), raw))


def convert(lines, *, run_id, root_id, origin, clock, model):
    def at(moment):
        return (origin + timedelta(seconds=moment - clock)).isoformat()

    events, open_tools, output, usage_seen = [], {}, None, False
    failure, boundary = None, clock
    for moment, raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind, item = event.get("type"), event.get("item") or {}
        item_type = item.get("type")
        if kind == "turn.started":
            boundary = moment
        elif kind == "item.started" and item_type in TOOL_ITEMS:
            if not open_tools and moment > boundary:
                # The model chose this tool call without emitting a visible item.
                events.append(
                    AgentEvent(
                        event_id="evt_" + uuid.uuid4().hex,
                        run_id=run_id,
                        parent_id=root_id,
                        event_type="model_call",
                        name="codex_tool_decision",
                        model=model,
                        started_at=at(boundary),
                        ended_at=at(moment),
                        duration_ms=(moment - boundary) * 1000,
                        token_provenance=UNAVAILABLE,
                        metadata={"codex_item_type": "tool_decision"},
                    )
                )
            open_tools[item.get("id")] = moment
        elif kind == "item.completed" and item_type in TOOL_ITEMS:
            started = open_tools.pop(item.get("id"), moment)
            exit_code = item.get("exit_code")
            failed = item.get("status") == "failed" or exit_code not in (None, 0)
            events.append(
                AgentEvent(
                    event_id="evt_" + uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=root_id,
                    event_type="tool_call",
                    name="shell" if item_type == "command_execution" else item_type,
                    started_at=at(started),
                    ended_at=at(moment),
                    duration_ms=(moment - started) * 1000,
                    input_text=clip(item.get("command") or json.dumps(item.get("changes"))),
                    output_text=clip(item.get("aggregated_output")),
                    status="error" if failed else "ok",
                    error=f"exit code {exit_code}" if failed else None,
                    metadata={"codex_item_type": item_type, "exit_code": exit_code},
                )
            )
            boundary = max(boundary, moment)
        elif kind == "item.completed" and item_type in MODEL_ITEMS:
            # The model was working from the previous visible boundary until this
            # item arrived, unless a tool was still running in that interval.
            started = max([boundary, *open_tools.values()])
            events.append(
                AgentEvent(
                    event_id="evt_" + uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=root_id,
                    event_type="model_call",
                    name=f"codex_{item_type}",
                    model=model,
                    started_at=at(started),
                    ended_at=at(moment),
                    duration_ms=(moment - started) * 1000,
                    token_provenance=UNAVAILABLE,
                    output_text=clip(item.get("text")),
                    metadata={"codex_item_type": item_type},
                )
            )
            if item_type == "agent_message":
                output = item.get("text")
            boundary = moment
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            models = [item for item in events if item.event_type == "model_call"]
            if not models:
                models.append(
                    AgentEvent(
                        event_id="evt_" + uuid.uuid4().hex,
                        run_id=run_id,
                        parent_id=root_id,
                        event_type="model_call",
                        name="codex_turn",
                        model=model,
                        started_at=at(boundary),
                        ended_at=at(moment),
                        duration_ms=(moment - boundary) * 1000,
                    )
                )
                events.append(models[0])
            last = models[-1]
            last.input_tokens = int(usage.get("input_tokens") or 0)
            last.output_tokens = int(usage.get("output_tokens") or 0)
            last.token_provenance = PROVIDER
            last.metadata.update(
                usage_scope="turn_total",
                cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                reasoning_output_tokens=int(usage.get("reasoning_output_tokens") or 0),
            )
            usage_seen = True
        elif kind in {"turn.failed", "error"}:
            detail = event.get("error") or event.get("message") or kind
            failure = detail.get("message", str(detail)) if isinstance(detail, dict) else detail
    return events, output, usage_seen, failure


def run(args, codex_args, request):
    codex = shutil.which(args.codex)
    if codex is None:
        raise SystemExit(f"Cannot find the Codex CLI executable {args.codex!r}")
    instructions = args.instructions.read_text(encoding="utf-8") if args.instructions else ""
    prompt = task_prompt(request["input"], instructions)
    command = [
        codex,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(args.cd.resolve()),
        *codex_args,
        "-",
    ]
    trace = AgentTrace(
        f"codex exec: {request['task_id']}",
        metadata={
            "integration": "codex_exec",
            "condition": args.condition,
            "task_id": request["task_id"],
            "repetition": request["repetition"],
            "codex_args": codex_args,
        },
    )
    root_id = "evt_" + uuid.uuid4().hex
    origin, clock = datetime.now(timezone.utc), time.perf_counter()
    process = subprocess.Popen(  # nosec B603 - explicit Codex argv, no shell
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = []
    reader = threading.Thread(target=read_stream, args=(process, lines), daemon=True)
    reader.start()
    stderr = threading.Thread(target=lambda: process.stderr.read(), daemon=True)
    stderr.start()
    process.stdin.write(prompt)
    process.stdin.close()
    returncode = process.wait()
    reader.join()
    ended = time.perf_counter()
    events, output, usage_seen, failure = convert(
        lines, run_id=trace.run_id, root_id=root_id, origin=origin, clock=clock, model=args.model
    )
    if returncode != 0 and failure is None:
        failure = f"codex exec exited with code {returncode}"
    trace.add_event(
        AgentEvent(
            event_id=root_id,
            run_id=trace.run_id,
            event_type="agent_step",
            name="codex_exec",
            started_at=origin.isoformat(),
            ended_at=(origin + timedelta(seconds=ended - clock)).isoformat(),
            duration_ms=(ended - clock) * 1000,
            input_text=prompt,
            output_text=clip(output),
            status="error" if failure else "ok",
            error=failure,
            metadata={"operation_kind": "agent", "usage_reported": usage_seen},
        )
    )
    for event in events:
        trace.add_event(event)
    trace.metadata.update(
        output=(output or "").strip() if output is not None else None,
        success=failure is None and output is not None,
    )
    trace.finish()
    trace.export_json(request["trace_path"])
    return 1 if failure else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cd", type=Path, default=Path("."), help="Directory Codex reads.")
    parser.add_argument("--codex", default="codex", help="Codex CLI executable.")
    parser.add_argument("--model", default=None, help="Model label recorded in the trace.")
    parser.add_argument("--condition", default="unlabelled", help="Label recorded in the trace.")
    parser.add_argument(
        "--instructions", type=Path, default=None, help="Text file prepended to each task prompt."
    )
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    codex_args = argv[split + 1 :]
    if args.model:
        codex_args = ["--model", args.model, *codex_args]
    request = json.loads(sys.stdin.readline())
    raise SystemExit(run(args, codex_args, request))


if __name__ == "__main__":
    main()
