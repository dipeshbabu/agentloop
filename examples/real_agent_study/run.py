"""Run one explicit study condition against a verified, already-started local server."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    # Support `python -I path/to/run.py` in the released-package environment.
    # Add only examples/, never the checkout containing the development package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_agent_study"

from agentloop import reset_runtime
from agentloop.findings import build_diagnosis
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import trace_agent

from .agent import PROMPT_VERSION, ToolAgent
from .api import LocalModel
from .scoring import grade

MODELS = {
    "baseline": {
        "id": "Qwen3-4B-Instruct-2507-Q4_K_M",
        "repository": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "revision": "a06e946bb6b655725eafa393f4a9745d460374c9",
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "sha256": "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
    },
    "candidate": {
        "id": "Qwen2.5-1.5B-Instruct-Q4_K_M",
        "repository": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
    },
}
IMPLEMENTATION_FILES = ("agent.py", "api.py", "tools.py", "scoring.py", "run.py")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def implementation_hashes():
    return {name: file_hash(Path(__file__).with_name(name)) for name in IMPLEMENTATION_FILES}


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def freeze_pilot(source, destination):
    protocol = json.loads(Path(source).read_text(encoding="utf-8"))
    protocol.update(
        models=MODELS,
        implementation_hashes=implementation_hashes(),
        prompt_version=PROMPT_VERSION,
        server={
            "build": "b10964",
            "revision": "b29c606e2",
            "backend": "Vulkan",
            "ctx_size": 4096,
            "threads": 6,
            "batch_size": 256,
            "ubatch_size": 128,
            "parallel": 1,
            "gpu_layers": 99,
            "cache_prompt": False,
            "temperature": 0,
        },
        execution_order="Baseline block then candidate block within each repetition to archive each pair's original predictions before its candidate. Fixed condition order is a disclosed confound; no causal claim.",
    )
    write_new(destination, protocol)
    return protocol


def load_protocol(path, sources):
    protocol = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        protocol.get("schema_version") != "1.0"
        or protocol.get("implementation_hashes") != implementation_hashes()
        or protocol.get("models") != MODELS
    ):
        raise ValueError(
            "protocol or implementation identity mismatch; freeze a new protocol before execution"
        )
    ids = [task["id"] for task in protocol["tasks"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task IDs")
    for task in protocol["tasks"]:
        if (
            not isinstance(task["id"], str)
            or not re.fullmatch(r"(repository|sql|math)-[0-9]{2}", task["id"])
            or task["workload"] not in {"repository", "sql", "math"}
            or task["split"] not in {"pilot", "evaluation"}
            or not isinstance(task["prompt"], str)
            or not 1 <= len(task["prompt"]) <= 10000
        ):
            raise ValueError("invalid task declaration")
    for name, declaration in protocol["source_manifest"].items():
        if Path(name).name != name or file_hash(Path(sources) / name) != declaration["sha256"]:
            raise ValueError("frozen source checksum mismatch")
    return protocol


def verify_server(base_url, identity, model_file):
    # Validate the endpoint before requesting server metadata or inference.
    LocalModel(base_url, identity, max_tokens=1, timeout_s=5, seed=0)
    with urllib.request.urlopen(base_url.rstrip("/") + "/props", timeout=10) as response:  # nosec B310 - loopback validated
        props = json.load(response)
    if (
        Path(props["model_path"]).resolve() != Path(model_file).resolve()
        or file_hash(model_file) != identity["sha256"]
    ):
        raise ValueError("server is not using the condition's verified model artifact")
    return {
        "build_info": props.get("build_info"),
        "model_file": Path(model_file).name,
        "model_sha256": identity["sha256"],
        "total_slots": props.get("total_slots"),
    }


def run_case(protocol, task, condition, repetition, sources, out, base_url, *, traced=True):
    out = Path(out)
    if out.exists():
        raise ValueError("case output already exists; retain it and use an explicitly new attempt")
    protocol_hash = fingerprint(protocol)
    metadata = {
        "synthetic": False,
        "evidence_kind": "real_local_model_execution",
        "workload": task["workload"],
        "task_id": task["id"],
        "repetition": repetition,
        "seed": 20260923 + repetition,
        "protocol_hash": protocol_hash,
        "condition": condition,
        "source_revision": "agentloop-profiler:0.7.0",
        "framework": "langgraph:1.2.11" if task["workload"] == "repository" else "custom_python",
        "quality_criteria_version": protocol["scorer_version"],
    }
    write_new(
        out / "attempt.json",
        {
            **metadata,
            "status": "started",
            "implementation_hashes": protocol["implementation_hashes"],
        },
    )
    source = json.loads((Path(sources) / "agentloop-v070-source.json").read_text(encoding="utf-8"))
    model = LocalModel(
        base_url,
        protocol["models"][condition],
        max_tokens=protocol["max_output_tokens"],
        timeout_s=protocol["request_timeout_s"],
        seed=metadata["seed"],
    )
    setup_start = time.perf_counter()
    agent = ToolAgent(
        task["workload"],
        model,
        sources=source,
        red_csv=(Path(sources) / "winequality-red.csv").read_text(),
        white_csv=(Path(sources) / "winequality-white.csv").read_text(),
        max_calls=protocol["max_model_calls"],
        timeout_s=protocol["task_timeout_s"],
    )
    setup_ms = (time.perf_counter() - setup_start) * 1000
    state, trace, interrupted = None, None, None
    start = time.perf_counter()
    try:
        if traced:
            with trace_agent("real-study-" + task["workload"], metadata=metadata) as trace:
                model.trace = trace
                state = agent.run(task["prompt"])
        else:
            state = agent.run(task["prompt"])
    except BaseException as exc:
        interrupted = exc
        state = {
            "status": "interrupted" if not isinstance(exc, Exception) else "runner_error",
            "answer": None,
            "history": [],
            "error_category": type(exc).__name__,
        }
    elapsed_ms = (time.perf_counter() - start) * 1000
    output = {
        "answer": state["answer"],
        "tool_history": state["history"],
        "status": state["status"],
    }
    score = grade(state["answer"], task, sources=source, tool_history=state["history"])
    if state["status"] != "completed":
        score = {"score": 0.0, "passed": False, "detail": "task did not complete within its bounds"}
    receipt = {
        **metadata,
        "status": state["status"],
        "traced": traced,
        "elapsed_ms": elapsed_ms,
        "setup_ms": setup_ms,
        "quality": score,
        "output": output,
        "model_calls": model.receipts,
        "recording_ms": model.recording_ms + agent.tool_recording_ms,
        "operating_cost_usd": None,
        "paid_provider_spend_usd": 0,
    }
    if trace is not None:
        trace.metadata.update(
            output=output,
            success=score["passed"],
            quality_score=score["score"],
            task_status=state["status"],
        )
        trace.export_json(out / "trace.json")
        receipt["trace_hash"] = fingerprint(trace.to_dict())
        # These predictions are persisted during the baseline block, before any
        # candidate block can run; reporting must read this archived diagnosis.
        diagnosis = build_diagnosis(trace)
        write_new(out / "diagnosis.json", diagnosis)
        write_new(out / "plan.json", build_optimization_plan(trace))
        receipt["diagnosis_hash"] = fingerprint(diagnosis)
    write_new(out / "receipt.json", receipt)
    if interrupted is not None:
        raise interrupted
    return receipt


def run_block(
    protocol_path, root, condition, split, repetition, base_url, model_file, *, traced=True
):
    root = Path(root)
    protocol = load_protocol(protocol_path, root / "sources")
    if type(repetition) is not int or not 0 <= repetition < protocol["repetitions"]:
        raise ValueError("repetition is outside the frozen plan")
    if (
        importlib.metadata.version("agentloop-profiler") != protocol["agentloop_version"]
        or importlib.metadata.version("langgraph") != protocol["langgraph_version"]
    ):
        raise ValueError("study requires the declared released package and framework versions")
    if split == "evaluation" and protocol.get("phase") != "evaluation":
        raise ValueError("held-out execution requires the frozen evaluation protocol")
    reset_runtime()
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    tasks = [
        task
        for task in protocol["tasks"]
        if task["split"] == split
        and (split != "evaluation" or task["id"] in protocol["evaluation_task_ids"])
    ]
    tasks.sort(key=lambda item: fingerprint([item["id"], repetition]))
    mode = "traced" if traced else "untraced"
    block = root / "runs" / split / condition / f"rep-{repetition}-{mode}"
    if block.exists():
        raise ValueError("block already exists; incomplete attempts must remain explicit")
    if condition == "candidate" and traced:
        for task in tasks:
            baseline = root / "runs" / split / "baseline" / f"rep-{repetition}-traced" / task["id"]
            receipt = json.loads((baseline / "receipt.json").read_text())
            diagnosis = json.loads((baseline / "diagnosis.json").read_text())
            if receipt["protocol_hash"] != fingerprint(protocol) or receipt[
                "diagnosis_hash"
            ] != fingerprint(diagnosis):
                raise ValueError("candidate requires the original archived baseline predictions")
    server = verify_server(base_url, protocol["models"][condition], model_file)
    write_new(
        block / "schedule.json",
        {
            "protocol_hash": fingerprint(protocol),
            "condition": condition,
            "split": split,
            "repetition": repetition,
            "traced": traced,
            "task_ids": [task["id"] for task in tasks],
            "server": server,
            "python": platform.python_version(),
        },
    )
    for task in tasks:
        receipt = run_case(
            protocol,
            task,
            condition,
            repetition,
            root / "sources",
            block / task["id"],
            base_url,
            traced=traced,
        )
        print(
            json.dumps(
                {
                    "task": task["id"],
                    "condition": condition,
                    "status": receipt["status"],
                    "quality": receipt["quality"]["score"],
                    "elapsed_s": round(receipt["elapsed_ms"] / 1000, 3),
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-pilot")
    freeze.add_argument("source", type=Path)
    freeze.add_argument("destination", type=Path)
    execute = sub.add_parser("run")
    execute.add_argument("--protocol", type=Path, required=True)
    execute.add_argument("--root", type=Path, required=True)
    execute.add_argument("--condition", choices=MODELS, required=True)
    execute.add_argument("--split", choices=("pilot", "evaluation"), required=True)
    execute.add_argument("--repetition", type=int, default=0)
    execute.add_argument("--base-url", default="http://127.0.0.1:8766")
    execute.add_argument("--model-file", type=Path, required=True)
    execute.add_argument("--untraced", action="store_true")
    execute.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze-pilot":
        freeze_pilot(args.source, args.destination)
    elif not args.execute:
        parser.error("real inference requires --execute")
    else:
        run_block(
            args.protocol,
            args.root,
            args.condition,
            args.split,
            args.repetition,
            args.base_url,
            args.model_file,
            traced=not args.untraced,
        )


if __name__ == "__main__":
    main()
