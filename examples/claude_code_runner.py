"""Run Claude Code on guided tasks and record a native AgentLoop trace.

The runner receives the standard `agentloop improve capture` JSON request on
stdin. It runs ``claude -p --output-format stream-json --verbose`` with only
read-only tools allowed, timestamps each JSON line as it arrives, and converts
the stream into AgentLoop events:

- each Claude API response (one ``message.id``) is one model span. It starts at
  the session start or when the last tool result arrived, and ends when the last
  part of that response arrives, so text and tool requests in one response are
  one model call;
- each ``tool_use`` block is a tool span that ends when its ``tool_result``
  arrives, keeping errors reported by the tool;
- Claude reports usage per response. Input tokens include cache reads and cache
  writes, which are also recorded separately, with ``usage_scope = "request"``.

The ``result`` event's text is the task answer that the frozen checks score.
Arguments after ``--`` are passed to ``claude`` unchanged, so a candidate can
change the model, system prompt or settings without editing this file.

Example:

    agentloop improve capture --tasks examples/codex_repository_tasks.json \
        --out runs/claude-baseline -- python examples/claude_code_runner.py --cd .
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 - runs the operator's Claude Code CLI with explicit argv
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.tokens import PROVIDER
from agentloop.tracer import AgentTrace

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
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


def result_text(content):
    """Tool results are a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return None


def read_stream(process, lines):
    """Timestamp each JSON line on arrival; stream events carry no timestamps."""
    for raw in process.stdout:
        lines.append((time.perf_counter(), raw))


def convert(lines, *, run_id, root_id, origin, clock, model):
    def at(moment):
        return (origin + timedelta(seconds=moment - clock)).isoformat()

    events, responses, open_tools = [], {}, {}
    output, failure, details, boundary = None, None, {}, clock
    for moment, raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            boundary = moment
            model = model or event.get("model")
            details["claude_session_id"] = event.get("session_id")
        elif kind == "assistant":
            message = event.get("message") or {}
            key = message.get("id") or f"anonymous-{len(responses)}"
            span = responses.get(key)
            if span is None:
                span = AgentEvent(
                    event_id="evt_" + uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=root_id,
                    event_type="model_call",
                    name="claude_response",
                    model=message.get("model") or model,
                    started_at=at(boundary),
                    ended_at=at(moment),
                    duration_ms=(moment - boundary) * 1000,
                    metadata={"content_types": [], "start": boundary},
                )
                responses[key] = span
                events.append(span)
            span.ended_at = at(moment)
            span.duration_ms = (moment - span.metadata["start"]) * 1000
            usage = message.get("usage")
            if isinstance(usage, dict):
                # Each part of a response repeats the same usage: set, never add.
                cache_read = int(usage.get("cache_read_input_tokens") or 0)
                cache_write = int(usage.get("cache_creation_input_tokens") or 0)
                span.input_tokens = int(usage.get("input_tokens") or 0) + cache_read + cache_write
                span.output_tokens = int(usage.get("output_tokens") or 0)
                span.token_provenance = PROVIDER
                span.metadata.update(
                    usage_scope="request",
                    cached_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_write,
                )
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                span.metadata["content_types"].append(block.get("type"))
                if block.get("type") == "text":
                    span.output_text = clip(block.get("text"))
                elif block.get("type") == "tool_use":
                    open_tools[block.get("id")] = (moment, block)
        elif kind == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                started, request = open_tools.pop(block.get("tool_use_id"), (moment, {}))
                failed = block.get("is_error") is True
                text = result_text(block.get("content"))
                events.append(
                    AgentEvent(
                        event_id="evt_" + uuid.uuid4().hex,
                        run_id=run_id,
                        parent_id=root_id,
                        event_type="tool_call",
                        name=request.get("name") or "tool",
                        started_at=at(started),
                        ended_at=at(moment),
                        duration_ms=(moment - started) * 1000,
                        input_text=clip(json.dumps(request.get("input"), sort_keys=True))
                        if "input" in request
                        else None,
                        output_text=clip(text),
                        status="error" if failed else "ok",
                        error=(clip(text) or "tool error") if failed else None,
                        metadata={"claude_tool_use_id": block.get("tool_use_id")},
                    )
                )
                boundary = max(boundary, moment)
        elif kind == "result":
            if isinstance(event.get("result"), str):
                output = event["result"]
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                failure = (
                    event.get("result")
                    if isinstance(event.get("result"), str) and event.get("result")
                    else event.get("subtype") or "Claude Code reported an error"
                )
            details.update(
                claude_num_turns=event.get("num_turns"),
                claude_duration_ms=event.get("duration_ms"),
                claude_duration_api_ms=event.get("duration_api_ms"),
                claude_total_cost_usd=event.get("total_cost_usd"),
            )
    for span in responses.values():
        span.metadata.pop("start", None)
    usage_seen = any(span.token_provenance == PROVIDER for span in responses.values())
    return events, output, usage_seen, failure, details


def run(args, claude_args, request):
    claude = shutil.which(args.claude)
    if claude is None:
        raise SystemExit(f"Cannot find the Claude Code CLI executable {args.claude!r}")
    instructions = args.instructions.read_text(encoding="utf-8") if args.instructions else ""
    prompt = task_prompt(request["input"], instructions)
    command = [
        claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        ",".join(READ_ONLY_TOOLS),
        *claude_args,
    ]
    trace = AgentTrace(
        f"claude code: {request['task_id']}",
        metadata={
            "integration": "claude_code",
            "condition": args.condition,
            "task_id": request["task_id"],
            "repetition": request["repetition"],
            "claude_args": claude_args,
        },
    )
    root_id = "evt_" + uuid.uuid4().hex
    origin, clock = datetime.now(timezone.utc), time.perf_counter()
    process = subprocess.Popen(  # nosec B603 - explicit Claude Code argv, no shell
        command,
        cwd=args.cd.resolve(),
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
    events, output, usage_seen, failure, details = convert(
        lines, run_id=trace.run_id, root_id=root_id, origin=origin, clock=clock, model=args.model
    )
    if returncode != 0 and failure is None:
        failure = f"claude exited with code {returncode}"
    trace.add_event(
        AgentEvent(
            event_id=root_id,
            run_id=trace.run_id,
            event_type="agent_step",
            name="claude_code",
            started_at=origin.isoformat(),
            ended_at=(origin + timedelta(seconds=ended - clock)).isoformat(),
            duration_ms=(ended - clock) * 1000,
            input_text=prompt,
            output_text=clip(output),
            status="error" if failure else "ok",
            error=failure,
            metadata={"operation_kind": "agent", "usage_reported": usage_seen, **details},
        )
    )
    for event in events:
        trace.add_event(event)
    trace.metadata.update(
        output=output.strip() if output is not None else None,
        success=failure is None and output is not None,
    )
    trace.finish()
    trace.export_json(request["trace_path"])
    return 1 if failure else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cd", type=Path, default=Path("."), help="Directory Claude Code reads.")
    parser.add_argument("--claude", default="claude", help="Claude Code CLI executable.")
    parser.add_argument("--model", default=None, help="Model passed to Claude Code and recorded.")
    parser.add_argument("--condition", default="unlabelled", help="Label recorded in the trace.")
    parser.add_argument(
        "--instructions", type=Path, default=None, help="Text file prepended to each task prompt."
    )
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    claude_args = argv[split + 1 :]
    if args.model:
        claude_args = ["--model", args.model, *claude_args]
    request = json.loads(sys.stdin.readline())
    raise SystemExit(run(args, claude_args, request))


if __name__ == "__main__":
    main()
