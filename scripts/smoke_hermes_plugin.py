"""Run an exported Hermes plugin with the standard library only and no providers.

Use python -I -S scripts/smoke_hermes_plugin.py --plugin PATH --out PATH.
The synthetic host follows the pinned Hermes 0.21.6 keyword hook contract.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType


class Host:
    def __init__(self, output: Path):
        self.output = output
        self.hooks = {}

    def get_config(self, name, default=None):
        return str(self.output) if name == "export_dir" else default

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload = callback

    def emit(self, hook, **data):
        if self.hooks[hook](**data) is not None:
            raise RuntimeError("Observer hook changed host behavior")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Use python -I -S to exclude site packages and source imports")
    spec = importlib.util.spec_from_file_location(
        "hermes_agentloop_smoke",
        args.plugin / "__init__.py",
        submodule_search_locations=[str(args.plugin.resolve())],
    )
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: args.out.resolve()
    sys.modules["hermes_constants"] = constants
    spec.loader.exec_module(plugin)
    host = Host(args.out.resolve())
    plugin.register(host)
    identity = {
        "session_id": "synthetic-session",
        "task_id": "synthetic-task",
        "turn_id": "synthetic-turn",
    }
    host.emit("pre_llm_call", **identity, user_message="PRIVATE_PROMPT")
    started = time.time()
    host.emit("pre_api_request", **identity, api_request_id="request", started_at=started)
    ended = time.time()
    host.emit(
        "post_api_request",
        **identity,
        api_request_id="request",
        model="unknown-model",
        started_at=started,
        ended_at=ended,
        api_duration=ended - started,
        usage={
            "input_tokens": 3,
            "cache_read_tokens": 1,
            "cache_write_tokens": 0,
            "prompt_tokens": 4,
            "output_tokens": 2,
            "reasoning_tokens": 1,
        },
    )
    host.emit(
        "post_tool_call",
        **identity,
        tool_name="local",
        tool_call_id="tool",
        status="success",
        duration_ms=0,
        args={"query": "PRIVATE_ARGUMENT"},
        result="PRIVATE_RESULT",
    )
    host.emit("post_llm_call", **identity, assistant_response="PRIVATE_FINAL")
    host.emit("on_session_end", **identity, completed=True)
    host.unload()
    files = list(args.out.glob("run_hermes_*.json"))
    if len(files) != 1 or any(
        name == "agentloop" or name.startswith("agentloop.") for name in sys.modules
    ):
        raise RuntimeError(
            "Standalone plugin did not export exactly one trace without AgentLoop imports"
        )
    raw = files[0].read_text(encoding="utf-8")
    trace = json.loads(raw)
    if "PRIVATE_" in raw or trace["events"][1]["input_tokens"] != 4:
        raise RuntimeError("Privacy or normalized-accounting regression")
    # Label this protocol fixture before it is analyzed outside the isolated process.
    trace["metadata"]["synthetic"] = True
    trace["metadata"]["source"] = "agentloop_hermes_plugin_smoke"
    files[0].write_text(json.dumps(trace, indent=2), encoding="utf-8")
    print(files[0])


if __name__ == "__main__":
    main()
