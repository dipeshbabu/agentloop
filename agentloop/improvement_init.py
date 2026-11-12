"""Create a small reviewed Python adapter and data-only guided project config."""

from __future__ import annotations

import json
import keyword
from pathlib import Path
from string import Template

from agentloop.improvement import require

EXAMPLE = '''"""Offline tool example. Replace answer() with your existing agent."""
import hashlib

from agentloop import trace_tool_call

NOTES = {"tracing": "Record each operation.", "quality": "Check the final answer."}


def lookup_note(key):
    with trace_tool_call("lookup_note", metadata={"parallel_safe": True}):
        # Local CPU work makes repeated execution visible without a model API.
        hashlib.pbkdf2_hmac("sha256", key.encode(), b"agentloop-example", 40000)
        return NOTES[key]


def answer(payload):
    key = payload["key"]
    # Review the repeated calls, then reuse one lookup result for this request.
    return {"notes": [lookup_note(key) for _ in range(3)]}
'''

RUNNER = Template('''"""Adapter for $target_label. Inspect this file before running it."""
import argparse
import asyncio
import inspect
import json
import sys
import traceback

import agentloop


async def resolve_result(value):
    return await value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the agent import and callable.")
    parser.add_argument("--debug", action="store_true", help="Show the agent exception traceback.")
    args = parser.parse_args()
    try:
        import $module as connected_agent
        answer = connected_agent.$function
        if not callable(answer):
            raise TypeError("The selected target is not callable")
    except Exception as error:
        print("Cannot connect $target_label (" + type(error).__name__ + "). "
              "Install your agent in this Python environment or edit the import in this runner.",
              file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 2
    if args.check:
        print("Connected $target_label")
        return 0
    try:
        request = json.load(sys.stdin)
        output_path = request["trace_path"]
        payload = request["input"]
    except (ValueError, TypeError, KeyError):
        print("Expected an improve capture request on stdin. Run agentloop improve capture -- python agentloop_runner.py.", file=sys.stderr)
        return 2
    failure = None
    with agentloop.trace_agent($trace_name) as trace:
        $source_marker
        try:
            output = answer(payload)
            if inspect.isawaitable(output):
                output = asyncio.run(resolve_result(output))
            json.dumps(output, allow_nan=False)
            trace.metadata["output"] = output
        except BaseException as error:
            failure = error
            trace.metadata.update(success=False, output_unavailable=True)
    trace.export_json(output_path)
    if failure is not None:
        print("Agent call failed (" + type(failure).__name__ + "). "
              "Return finite JSON from $target_label. Use --show-runner-output with this runner's --debug for details.",
              file=sys.stderr)
        if args.debug:
            traceback.print_exception(failure)
        return 130 if isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError)) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''')


def project_tasks(project: Path) -> Path:
    """Resolve task data from an explicitly selected project configuration."""
    import tomllib

    try:
        config = tomllib.loads(project.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            f"No project config at {project}. Pass --tasks FILE or run agentloop improve init."
        ) from None
    require(config.get("schema_version") == "1.0", "Project config schema_version must be 1.0")
    require(
        not set(config) - {"schema_version", "tasks"},
        "Project config supports only schema_version and tasks",
    )
    require(
        isinstance(config.get("tasks"), str) and bool(config["tasks"].strip()),
        "Set tasks to a JSON file path in the project config",
    )
    return project.resolve().parent / config["tasks"]


def initialize(root: Path, *, target: str | None = None, name: str | None = None):
    root = root.resolve()
    demo = target is None
    target = target or "agentloop_example:answer"
    require(target.count(":") == 1, "Use --agent package.module:callable")
    module, function = target.split(":")
    require(
        all(
            part.isidentifier() and not keyword.iskeyword(part)
            for value in (module, function)
            for part in value.split(".")
        ),
        "Agent module and callable must be Python identifiers separated by dots",
    )
    name = name or root.name
    require(isinstance(name, str) and bool(name.strip()), "Project name must be nonempty")
    files = {
        "agentloop.toml": 'schema_version = "1.0"\ntasks = "agentloop_tasks.json"\n',
        "agentloop_tasks.json": json.dumps(
            {
                "schema_version": "1.0",
                "name": name,
                "repetitions": 1,
                "tasks": [
                    {
                        "id": key,
                        "input": {"key": key},
                        "expected": {"notes": [value] * 3},
                        "scorer": {"type": "json_subset"},
                    }
                    for key, value in {
                        "tracing": "Record each operation.",
                        "quality": "Check the final answer.",
                    }.items()
                ],
            },
            indent=2,
        )
        + "\n",
        "agentloop_runner.py": RUNNER.substitute(
            module=module,
            function=function,
            target_label=target,
            trace_name=repr(name),
            source_marker='trace.metadata.update(synthetic=True, source="improve_init_offline_example")'
            if demo
            else 'trace.metadata["source"] = "improve_init_python_adapter"',
        ),
    }
    if demo:
        files["agentloop_example.py"] = EXAMPLE
    files["AGENTLOOP.md"] = f"""# Optimize {name}

The included task examples describe the offline tool example. For your existing
agent, replace their inputs and expected answers with independent correctness
criteria before capturing a baseline. The adapter calls `{target}` with one
JSON input value and records its final JSON output. Async callables are supported.

Run from this project directory in the Python environment containing AgentLoop
and your agent.

```bash
python agentloop_runner.py --check
agentloop improve capture --out baseline -- python agentloop_runner.py
agentloop improve start --baseline baseline/capture.json --out optimization
```

Review `optimization/plan.md` and the original evidence. For the included example,
edit `answer()` to reuse one lookup result within the request.

```python
value = lookup_note(key)
return {{"notes": [value] * 3}}
```

For your own agent, review and apply a change appropriate to its evidence.

```bash
agentloop improve capture --out candidate -- python agentloop_runner.py
agentloop improve finish --session optimization/session.json --candidate candidate/capture.json --change "Reuse the lookup result" --out results
```

Open `results/report.html`. A rejected comparison returns exit code 1 and retains
its report. To inspect an agent failure, use `--show-runner-output` on capture
and pass `--debug` after the runner filename. Traces retain final outputs for
quality verification. Review artifacts before sharing them.

The offline example executes real local CPU work on fictional notes and is marked
synthetic. Its timings illustrate this example. Your agent needs representative
tasks and quality checks. The project config selects task data; the argv after
`--` selects executable code.
"""
    require(root.is_dir() or not root.exists(), "--out must name a project directory")
    collisions = [
        str(root / filename)
        for filename in files
        if (root / filename).exists() or (root / filename).is_symlink()
    ]
    require(
        not collisions,
        "Initialization would overwrite existing files: "
        + ", ".join(collisions)
        + ". Choose another --out directory.",
    )
    root.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for filename, content in files.items():
            path = root / filename
            with path.open("x", encoding="utf-8", newline="\n") as destination:
                created.append((path, path.stat().st_ino))
                destination.write(content)
    except BaseException:
        for path, inode in reversed(created):
            # Remove only files created by this attempt, including partial writes.
            if not path.is_symlink() and path.is_file() and path.stat().st_ino == inode:
                path.unlink()
        raise
    return list(files)
