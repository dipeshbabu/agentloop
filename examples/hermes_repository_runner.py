"""Answer repository metadata questions through real Hermes and local inference.

This runner receives the standard `improve capture` JSON request on stdin. The
baseline repairs a free-text answer into the required JSON contract. The candidate
uses constrained JSON from its first turn and the frozen external quality checks.
The validated variant also checks values against the local source. Public
repository context and model responses are retained for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def check_repository_answer(context, question, response):
    """Check structured metadata against its authoritative local TOML source."""
    match = re.search(r"\bproject\.([A-Za-z0-9_-]+)", question)
    if match is None:
        raise ValueError("This example requires a question naming a project metadata field")
    field = match.group(1)
    value = tomllib.loads(context)["project"][field]
    if field == "authors":
        value = ", ".join(author["name"] for author in value)
    if not isinstance(value, str):
        raise ValueError("This example answers string-valued metadata questions")
    try:
        observed = json.loads(response or "")
        valid = isinstance(observed, dict) and observed.get("answer") == value
    except (ValueError, TypeError):
        valid = False
    final = response if valid else json.dumps({"answer": value})
    return final, {"field": field, "model_answer_valid": valid, "repair_applied": not valid}


def run(args, request):
    output = Path(request["trace_path"]).resolve()
    profile = output.parent / "profiles" / uuid.uuid4().hex
    profile.mkdir(parents=True)
    keep = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    environment = {key: value for key, value in os.environ.items() if key in keep}
    environment.update(
        HERMES_HOME=str(profile),
        HERMES_SINGLE_QUERY_SESSION="1",
        TERMINAL_ENV="local",
        TERMINAL_CWD=str(profile),
        NO_PROXY="127.0.0.1,localhost",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    os.environ.clear()
    os.environ.update(environment)
    os.chdir(profile)
    import shutil

    shutil.copytree(args.plugin_dir, profile / "plugins/agentloop")
    (profile / "config.yaml").write_text(
        json.dumps(
            {
                "model": {
                    "default": "agentloop-local-qwen",
                    "provider": "openai",
                    "base_url": args.base_url,
                    "context_length": 65536,
                },
                "plugins": {
                    "enabled": ["agentloop"],
                    "entries": {"agentloop": {"settings": {"capture_content": True}}},
                },
            }
        ),
        encoding="utf-8",
    )
    parsed = urlsplit(args.base_url)
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("This example requires the owned loopback model provider")
    # Import the shared host check and socket guard from this checkout's scripts.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from smoke_hermes_runtime import install_socket_guard, source_info

    host = source_info(args.hermes_root)
    blocked = install_socket_guard(parsed.port)
    import run_agent
    from hermes_cli.plugins import get_plugin_manager
    from run_agent import AIAgent

    if not Path(run_agent.__file__).resolve().is_relative_to(args.hermes_root):
        raise ValueError("Unexpected Hermes installation")
    manager = get_plugin_manager()
    manager.discover_and_load()
    agent = AIAgent(
        base_url=args.base_url,
        api_key="local-pilot-key",
        provider="openai",
        api_mode="chat_completions",
        model="agentloop-local-qwen",
        max_iterations=4,
        max_tokens=96,
        request_overrides={
            "temperature": 0,
            "seed": 20261112,
            "extra_body": {"cache_prompt": False},
        },
        enabled_toolsets=[],
        quiet_mode=True,
        session_id="guided-" + uuid.uuid4().hex,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        load_soul_identity=False,
        run_budget_seconds=120,
        cwd=str(profile),
    )
    prompts, results = [], []
    repair, check = None, None
    started, clock = timestamp(), time.perf_counter()
    try:
        context = args.source.read_text(encoding="utf-8")
        question = request["input"]["question"]
        prefix = f"Repository pyproject.toml\n{context}\n\nQuestion: {question}\n"
        structured = (
            prefix
            + "Return only a JSON object with one string field named answer. Copy the exact requested value."
        )
        prompt = prefix + "Reply with only the requested value as plain text."
        if args.condition != "baseline":
            prompt = structured
            agent.request_overrides["response_format"] = {"type": "json_object"}
        if args.condition == "wrong":
            prompt = 'Return only {"answer":"WRONG"}.'
        prompts.append(prompt)
        results.append(agent.run_conversation(prompt))
        try:
            initial = json.loads(results[-1].get("final_response") or "")
            valid = isinstance(initial, dict) and isinstance(initial.get("answer"), str)
        except (TypeError, ValueError):
            valid = False
        if args.condition == "baseline" and not valid:
            repair = {"event_id": "evt_" + uuid.uuid4().hex, "started_at": timestamp()}
            repair_clock = time.perf_counter()
            agent.request_overrides["response_format"] = {"type": "json_object"}
            prompts.append(structured)
            results.append(agent.run_conversation(structured))
            repair.update(
                ended_at=timestamp(), duration_ms=(time.perf_counter() - repair_clock) * 1000
            )
        final = results[-1].get("final_response")
        if args.condition == "validated":
            check = {"event_id": "evt_" + uuid.uuid4().hex, "started_at": timestamp()}
            check_clock = time.perf_counter()
            final, facts = check_repository_answer(context, question, final)
            check.update(
                ended_at=timestamp(),
                duration_ms=(time.perf_counter() - check_clock) * 1000,
                metadata={"operation_kind": "guardrail", **facts},
                output_text=final,
            )
    finally:
        ended, elapsed = timestamp(), (time.perf_counter() - clock) * 1000
        agent.close()
        manager.unload()
    if blocked:
        raise ValueError("Unexpected outbound connection in the local workload")
    source_info(args.hermes_root)
    originals = sorted(
        (profile / "agentloop/traces").glob("*.json"),
        key=lambda path: json.loads(path.read_bytes())["started_at"],
    )
    if len(originals) != len(prompts):
        raise ValueError("Missing Hermes turn evidence")
    run_id, root_id = "run_guided_" + uuid.uuid4().hex, "evt_" + uuid.uuid4().hex
    events, sources, source_roots, complete = [], [], [], True
    for index, (path, prompt) in enumerate(zip(originals, prompts, strict=True)):
        raw = path.read_bytes()
        trace = json.loads(raw)
        capture = trace["metadata"]["hermes"]
        complete = (
            complete
            and capture["capture_complete"] is True
            and capture["turn_outcome"] == "completed"
        )
        sources.append({"run_id": trace["run_id"], "sha256": hashlib.sha256(raw).hexdigest()})
        for event in trace["events"]:
            event["metadata"]["source_run_id"] = event["run_id"]
            event["run_id"] = run_id
            if event["parent_id"] is None:
                source_roots.append(event["event_id"])
                event["parent_id"] = repair["event_id"] if index == 1 and repair else root_id
            if event["event_type"] == "model_call":
                event["input_text"] = prompt
                event["metadata"]["input_capture_source"] = "owned_public_repository_example"
            events.append(event)
    if repair:
        repair["metadata"] = {
            "reason": "initial_response_did_not_match_json_answer_contract",
            "depends_on": source_roots[:1],
        }
        events.append(
            {
                **repair,
                "run_id": run_id,
                "event_type": "retry",
                "name": "repair_output_format",
                "parent_id": root_id,
                "status": "ok",
            }
        )
    if check:
        check["metadata"]["depends_on"] = source_roots[-1:]
        events.append(
            {
                **check,
                "run_id": run_id,
                "event_type": "tool_call",
                "name": "validate_repository_answer",
                "parent_id": root_id,
                "status": "ok",
            }
        )
    events.insert(
        0,
        {
            "event_id": root_id,
            "run_id": run_id,
            "event_type": "tool_call",
            "name": "repository_question",
            "started_at": started,
            "ended_at": ended,
            "duration_ms": elapsed,
            "parent_id": None,
            "status": "ok" if complete else "error",
            "metadata": {"operation_kind": "workflow"},
        },
    )
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "name": "Hermes repository question",
        "started_at": started,
        "ended_at": ended,
        "elapsed_ms": elapsed,
        "events": events,
        "metadata": {
            "integration": "hermes",
            "output": final,
            "hermes": {
                "capture_complete": complete,
                "turn_outcome": "completed" if complete else "incomplete",
                "unfinished_requests": 0 if complete else 1,
                "events_dropped": 0,
                "source_turns": sources,
            },
            "source": "local_hermes_repository_workload",
            "host": host,
            "task_id": request["task_id"],
            "repetition": request["repetition"],
            "source_file_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        },
    }
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--condition", choices=("baseline", "candidate", "validated", "wrong"), required=True
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.source = arguments.source.resolve()
    arguments.hermes_root = arguments.hermes_root.resolve()
    arguments.plugin_dir = arguments.plugin_dir.resolve()
    run(arguments, json.load(sys.stdin))
