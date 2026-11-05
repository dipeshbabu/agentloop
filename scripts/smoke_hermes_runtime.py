"""Exercise an exported observer in a real, pinned Hermes one-shot runtime.

Run this with Hermes's Python, then validate its artifacts with AgentLoop's
Python using validate_hermes_runtime.py. The provider is a local deterministic
fixture; no model quality, billing, or application-performance claim is made.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess  # nosec B404 - fixed local git commands against the supplied fixture
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES_REVISION = "b200f5bc60e31588ba2cb60e288029c4a54ba5fa"
MODEL = "agentloop-local-pilot"
KEY = "local-pilot-key"
ANSWER = "AGENTLOOP_RUNTIME_PILOT_OK"
DOCUMENT = "AGENTLOOP_PILOT_DOCUMENT"
CHILD_ANSWER = "CHILD_PILOT_OK"
PROMPT_CANARY = "PRIVATE_PROMPT_CANARY"
ERROR_CANARY = "PRIVATE_PROVIDER_ERROR_CANARY"
OUTPUT_CANARY = "PRIVATE_OUTPUT_CANARY"
SCENARIOS = (
    "baseline",
    "stream",
    "tool",
    "retry",
    "interrupt",
    "tool-error",
    "privacy",
    "delegation",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_info(repository: Path) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(  # nosec B603 B607 - fixed git argv, no shell
            ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
            cwd=repository,
            text=True,
            timeout=90,
        ).strip()

    require(git("rev-parse", "HEAD") == HERMES_REVISION, "Use the pinned Hermes v0.22.7 checkout")
    require(
        not git("status", "--porcelain", "--untracked-files=no"), "Pinned Hermes source changed"
    )
    return {
        "revision": HERMES_REVISION,
        "tag": "v0.22.7",
        "lock_sha256": sha256(repository / "uv.lock"),
    }


def address_allowed(address, port: int) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    try:
        return ipaddress.ip_address(address[0]).is_loopback and int(address[1]) == port
    except (ValueError, TypeError):
        return False


def install_socket_guard(port: int) -> list[str]:
    """Limit Python socket clients to this fixture port; this is not an OS sandbox."""
    blocked: list[str] = []
    connect, connect_ex, resolve = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    )

    def guarded_connect(sock, address):
        if not address_allowed(address, port):
            blocked.append(str(address))
            raise OSError("Hermes pilot permits only its local provider")
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not address_allowed(address, port):
            blocked.append(str(address))
            raise OSError("Hermes pilot permits only its local provider")
        return connect_ex(sock, address)

    def guarded_resolve(host, service, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1"):
            blocked.append(str(host))
            raise socket.gaierror("Hermes pilot permits only its local provider")
        return resolve(host, service, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_resolve
    return blocked


class LocalProvider:
    """Owned responses for real Hermes model/tool dispatch, including SSE usage."""

    def __init__(self, scenario: str, fixture: Path):
        self.scenario, self.fixture = scenario, fixture
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.fatal = threading.Event()
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    self.reply(
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": MODEL,
                                    "object": "model",
                                    "owned_by": "local-pilot",
                                    "context_length": 131072,
                                }
                            ],
                        }
                    )
                elif self.path == "/props":
                    self.reply({"default_generation_settings": {"n_ctx": 131072}})
                else:
                    self.reply({"error": "unsupported discovery endpoint"}, 404)

            def reply(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024 * 1024:
                        return self.reply({"error": "invalid request length"}, 413)
                    data = json.loads(self.rfile.read(length))
                    if self.path == "/api/show":
                        with owner.lock:
                            owner.requests.append({"kind": "discovery", "path": self.path})
                        return self.reply(
                            {
                                "model_info": {
                                    "general.architecture": "llama",
                                    "llama.context_length": 131072,
                                }
                            }
                        )
                    if not self.path.endswith("/chat/completions"):
                        return self.reply({"error": "expected chat completions"}, 404)
                    names = sorted(
                        item.get("function", {}).get("name", "") for item in data.get("tools", [])
                    )
                    actor = (
                        "child"
                        if owner.scenario == "delegation" and "delegate_task" not in names
                        else "parent"
                    )
                    users = [
                        item.get("content")
                        for item in data.get("messages", [])
                        if item.get("role") == "user"
                    ]
                    record = {
                        "kind": "completion",
                        "path": self.path,
                        "actor": actor,
                        "model": data.get("model"),
                        "stream": data.get("stream", False),
                        "tools": names,
                        "user_input_sha256": hashlib.sha256(
                            json.dumps(users, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                    with owner.lock:
                        owner.requests.append(record)
                        count = sum(item["kind"] == "completion" for item in owner.requests)
                    if owner.scenario == "retry" and count == 1:
                        record["status"] = 429
                        return self.reply(
                            {
                                "error": {
                                    "message": ERROR_CANARY,
                                    "type": "rate_limit_error",
                                    "code": "rate_limit_exceeded",
                                }
                            },
                            429,
                        )
                    require(count <= 12, "Fixture request limit exceeded")
                    message, finish = owner.plan(data, actor)
                    record["status"] = 200
                    response = {
                        "id": f"chatcmpl-pilot-{count}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": MODEL,
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
                    }
                    if not data.get("stream"):
                        return self.reply(response)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    delta = dict(message)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [
                            {"index": index, **call}
                            for index, call in enumerate(delta["tool_calls"])
                        ]
                    parts = [(delta, None), ({}, finish)]
                    if owner.scenario == "interrupt":
                        parts = [
                            ({"role": "assistant", "content": "PARTIAL_PILOT_OUTPUT"}, None),
                            ({"content": " more"}, None),
                            ({}, "stop"),
                        ]
                    for part, reason in parts:
                        chunk = {
                            "id": response["id"],
                            "object": "chat.completion.chunk",
                            "created": response["created"],
                            "model": MODEL,
                            "choices": [{"index": 0, "delta": part, "finish_reason": reason}],
                        }
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                        self.wfile.flush()
                        if owner.scenario == "interrupt":
                            time.sleep(0.05)
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps(
                                {**response, "object": "chat.completion.chunk", "choices": []}
                            )
                            + "\n\ndata: [DONE]\n\n"
                        ).encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A real stream interruption closes this owned connection.
                except Exception as error:
                    owner.errors.append(str(error))
                    owner.fatal.set()
                    self.reply(
                        {
                            "error": {
                                "message": "local fixture failed",
                                "type": "invalid_request_error",
                            }
                        },
                        400,
                    )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def call(name: str, arguments: dict) -> tuple[dict, str]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_pilot_" + name,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }, "tool_calls"

    def plan(self, request: dict, actor: str) -> tuple[dict, str]:
        tools = {item.get("function", {}).get("name") for item in request.get("tools", [])}
        results = {
            item.get("tool_call_id"): item.get("content", "")
            for item in request.get("messages", [])
            if item.get("role") == "tool"
        }
        if self.scenario == "delegation" and actor == "parent":
            if "call_pilot_delegate_task" not in results:
                require("delegate_task" in tools, "Hermes did not advertise delegation")
                return self.call(
                    "delegate_task",
                    {
                        "tasks": [
                            {
                                "goal": "Read the fixture and report CHILD_PILOT_OK.",
                                "context": str(self.fixture),
                            }
                        ]
                    },
                )
            require(
                CHILD_ANSWER in str(results["call_pilot_delegate_task"]),
                "Delegation did not return the actual child result",
            )
        elif self.scenario in ("tool", "tool-error") or (
            self.scenario == "delegation" and actor == "child"
        ):
            if "call_pilot_read_file" not in results:
                require("read_file" in tools, "Hermes did not advertise read_file")
                path = (
                    self.fixture.parent / "missing.txt"
                    if self.scenario == "tool-error"
                    else self.fixture
                )
                return self.call("read_file", {"path": str(path)})
            if self.scenario == "tool-error":
                return {"role": "assistant", "content": "TOOL_FAILED_AS_EXPECTED"}, "stop"
            require(
                DOCUMENT in str(results["call_pilot_read_file"]),
                "The real file tool did not return the fixture",
            )
            if actor == "child":
                return {"role": "assistant", "content": CHILD_ANSWER}, "stop"
        return {
            "role": "assistant",
            "content": OUTPUT_CANARY if self.scenario == "privacy" else ANSWER,
        }, "stop"


def run_case(plugin: Path, repository: Path, output: Path, scenario: str) -> dict:
    require(sys.platform == "linux", "Run the host pilot on Linux, including WSL")
    require(scenario in SCENARIOS, "Unknown scenario")
    host = source_info(repository)
    require(
        not plugin.is_relative_to(output) and not output.is_relative_to(plugin),
        "Plugin input and output overlap",
    )
    require(
        all(
            (plugin / name).is_file() and not (plugin / name).is_symlink()
            for name in ("plugin.yaml", "recorder.py", "__init__.py", "LICENSE")
        ),
        "Expected a standalone exported plugin",
    )
    require(not output.exists(), "Choose a fresh pilot output directory")
    output.mkdir(parents=True)
    profile, work = output / "profile", output / "work"
    profile.mkdir()
    work.mkdir()
    if scenario != "baseline":
        (profile / "plugins").mkdir()
        destination = profile / "plugins/agentloop"
        destination.mkdir()
        for name in ("plugin.yaml", "recorder.py", "__init__.py", "LICENSE"):
            shutil.copyfile(plugin / name, destination / name)
    safe_keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in safe_keys}
    environment.update(
        {
            "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HERMES_HOME": str(profile),
            "HERMES_SINGLE_QUERY_SESSION": "1",
            "TERMINAL_ENV": "local",
            "TERMINAL_CWD": str(work),
            "NO_PROXY": "127.0.0.1,localhost",
            "HF_HOME": str(output / "cache/hf"),
            "TIKTOKEN_CACHE_DIR": str(output / "cache/tokenizer"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    os.environ.clear()
    os.environ.update(environment)
    os.chdir(work)
    fixture = work / "fixture.txt"
    fixture.write_text(DOCUMENT, encoding="utf-8")
    session_id = "agentloop-pilot-" + uuid.uuid4().hex
    delta_ready, finished = threading.Event(), threading.Event()
    callbacks: list[int] = []
    interrupted: list[bool] = []

    def on_delta(*args, **kwargs):
        callbacks.append(1)
        delta_ready.set()

    with LocalProvider(scenario, fixture) as provider:
        os.environ["OPENAI_API_KEY"] = KEY
        os.environ["OPENAI_BASE_URL"] = provider.base_url
        config = {
            "model": {"default": MODEL, "provider": "openai", "base_url": provider.base_url},
            "delegation": {
                "model": MODEL,
                "base_url": provider.base_url,
                "api_key": KEY,
                "api_mode": "chat_completions",
                "child_timeout_seconds": 30,
                "max_concurrent_children": 1,
                "max_spawn_depth": 1,
                "orchestrator_enabled": False,
                "fallback_providers": [],
            },
            "plugins": {
                "enabled": [] if scenario == "baseline" else ["agentloop"],
                "entries": {
                    "agentloop": {
                        "enabled": scenario != "baseline",
                        "settings": {} if scenario == "privacy" else {"capture_content": True},
                    }
                },
            },
        }
        (profile / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        blocked = install_socket_guard(provider.server.server_port)
        # These imports and dispatches are the real pinned host, not mock contexts.
        import run_agent
        from hermes_cli.plugins import get_plugin_manager
        from hermes_state import SessionDB
        from run_agent import AIAgent

        require(
            Path(run_agent.__file__).resolve().is_relative_to(repository),
            "A different Hermes installation was imported",
        )
        with contextlib.ExitStack() as stack:
            database = SessionDB(db_path=profile / "state.db")
            stack.callback(database.close)
            manager = get_plugin_manager()
            stack.callback(manager.unload)
            manager.discover_and_load()
            loaded = [entry for entry in manager.list_plugins() if entry["name"] == "agentloop"]
            if scenario != "baseline":
                require(
                    len(loaded) == 1 and not loaded[0]["error"] and loaded[0]["hooks"] == 10,
                    "Exported observer did not load in the real plugin manager",
                )
            toolsets = (
                ["file", "delegation"]
                if scenario == "delegation"
                else ["file"]
                if scenario in ("tool", "tool-error")
                else []
            )
            agent = AIAgent(
                base_url=provider.base_url,
                api_key=KEY,
                provider="openai",
                api_mode="chat_completions",
                model=MODEL,
                max_iterations=8,
                max_tokens=128,
                enabled_toolsets=toolsets,
                quiet_mode=True,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
                load_soul_identity=False,
                run_budget_seconds=60,
                cwd=str(work),
                session_db=database,
                stream_delta_callback=on_delta if scenario == "interrupt" else None,
            )
            stack.callback(agent.close)

            def stop_worker():
                while not finished.wait(0.01):
                    if provider.fatal.is_set() or (
                        scenario == "interrupt" and delta_ready.is_set()
                    ):
                        interrupted.append(agent.interrupt(hard_cancel=True))
                        return

            worker = threading.Thread(target=stop_worker, daemon=True)
            worker.start()
            started = time.perf_counter()
            try:
                result = agent.run_conversation(
                    PROMPT_CANARY + ": execute the local pilot task.", task_id="pilot-task"
                )
            finally:
                finished.set()
                worker.join(timeout=2)
            wall_ms = (time.perf_counter() - started) * 1000
        requests, errors = list(provider.requests), list(provider.errors)
    require(not errors, "Local provider fixture failed: " + "; ".join(errors))
    source_info(repository)
    raw_paths = sorted((profile / "agentloop/traces").glob("*.json"))
    native_dir = output / "native"
    native_dir.mkdir()
    artifacts = []
    for raw in raw_paths:
        payload = json.loads(raw.read_bytes())
        payload["metadata"].update(
            {
                "synthetic": True,
                "source": "hermes_runtime_pilot",
                "pilot": {
                    "scenario": scenario,
                    "synthetic_provider": True,
                    "hermes_revision": HERMES_REVISION,
                },
            }
        )
        native = native_dir / raw.name
        native.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        artifacts.append(
            {
                "raw": raw.relative_to(output).as_posix(),
                "raw_sha256": sha256(raw),
                "native": native.relative_to(output).as_posix(),
                "native_sha256": sha256(native),
            }
        )
    expected = (
        OUTPUT_CANARY
        if scenario == "privacy"
        else "TOOL_FAILED_AS_EXPECTED"
        if scenario == "tool-error"
        else ANSWER
    )
    report = {
        "schema_version": "1.0",
        "synthetic": True,
        "scenario": scenario,
        "host": host,
        "python_version": platform.python_version(),
        "plugin_recorder_sha256": sha256(plugin / "recorder.py"),
        "root_session_id": session_id,
        "completion_matches_fixture": result.get("final_response") == expected,
        "interrupted": result.get("interrupted") is True,
        "agent_error": bool(result.get("error")),
        "stream_callbacks": len(callbacks),
        "interrupt_calls": len(interrupted),
        "blocked_connections": blocked,
        "requests": requests,
        "artifacts": artifacts,
        "outer_turn_wall_ms": wall_ms,
        "timing_scope": "Local deterministic provider; diagnostic timings, not agent/model performance evidence",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "scenario": scenario,
                "recorded_traces": len(artifacts),
                "execution": "finished",
                "validation": "pending",
            }
        )
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True, type=Path)
    parser.add_argument("--hermes-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    args = parser.parse_args()
    run_case(
        args.plugin_dir.resolve(), args.hermes_root.resolve(), args.out.resolve(), args.scenario
    )


if __name__ == "__main__":
    main()
