"""Pilot then held-out guided comparisons, with retained counterbalanced controls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

import agentloop.improvement
from agentloop.experiment_types import fingerprint
from agentloop.improvement import capture, finish, start, write_once
from agentloop.studies import summarize_values

from .agent import LocalModel, execute

HERE = Path(__file__).resolve().parent


def task(name, workload, expected, **payload):
    return {
        "id": name,
        "input": {"workload": workload, **payload},
        "expected": {"answer": expected, "source": workload},
        "scorer": {"type": "json_subset"},
    }


def tasks(split):
    # Independent fixed labels. None of these expected values enters runner stdin.
    if split == "pilot":
        return [
            task(
                "repository-retry",
                "repository",
                "retry_delay",
                keyword="retry",
                question="Which function caps exponential retry backoff at thirty seconds?",
            ),
            task(
                "retrieval-retry",
                "retrieval",
                "3",
                document="retry-policy",
                question="What is the retry attempt limit? Copy only the numeric value.",
            ),
            task(
                "workflow-small",
                "workflow",
                "11",
                basket={"book": 1, "pen": 2},
                question="What is the total price of this basket? Return an integer string.",
            ),
        ]
    return [
        task(
            "repository-cache",
            "repository",
            "cache_key",
            keyword="Namespace",
            question="Which function namespaces a request cache entry by project?",
        ),
        task(
            "repository-completed",
            "repository",
            "is_complete",
            keyword="successfully",
            question="Which function recognizes successfully completed execution?",
        ),
        task(
            "retrieval-retention",
            "retrieval",
            "14",
            document="retention-policy",
            question="After how many days must debug traces be deleted? Copy only the numeric value.",
        ),
        task(
            "retrieval-context",
            "retrieval",
            "4096",
            document="context-policy",
            question="What is the request context token budget? Copy only the numeric value.",
        ),
        task(
            "workflow-pads",
            "workflow",
            "14",
            basket={"pad": 4, "pen": 1},
            question="What is the total price of this basket? Return an integer string.",
        ),
        task(
            "workflow-books",
            "workflow",
            "20",
            basket={"book": 2, "pad": 2},
            question="What is the total price of this basket? Return an integer string.",
        ),
    ]


def run(root, *, model_url=None, model_file=None, framework="python", repetitions=2):
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    if framework == "langgraph" and importlib.metadata.version("langgraph") != "1.2.11":
        raise ValueError(
            "This protocol requires langgraph==1.2.11; use uv run --with langgraph==1.2.11"
        )
    server = None
    if model_url:
        from examples.real_agent_study.run import MODELS, verify_server

        if model_file is None:
            raise ValueError(
                "--model-file is required to verify the loopback server's model identity"
            )
        server = verify_server(model_url, MODELS["candidate"], model_file)
    elif model_file is not None:
        raise ValueError("--model-file requires --model-url")
    if not 1 <= repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    implementation = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (HERE / "agent.py", HERE / "run.py", Path(agentloop.improvement.__file__))
    }
    implementation["local_model_client"] = hashlib.sha256(
        Path(sys.modules[LocalModel.__module__].__file__).read_bytes()
    ).hexdigest()
    protocol = {
        "schema_version": "1.0",
        "synthetic": model_url is None,
        "implementation_sha256": implementation,
        "python": platform.python_version(),
        "agentloop": importlib.metadata.version("agentloop-profiler"),
        "package_build": "Source checkout under release preparation; version metadata alone does not identify the published wheel",
        "platform": platform.platform(),
        "dependencies": dict(
            sorted(
                (item.metadata["Name"], item.version)
                for item in importlib.metadata.distributions()
                if item.metadata["Name"]
            )
        ),
        "decoding": {
            "temperature": 0,
            "seed": 20261112,
            "cache_prompt": False,
            "max_output_tokens": 96,
            "request_timeout_seconds": 60,
        },
        "framework": framework,
        "langgraph": "1.2.11" if framework == "langgraph" else None,
        "server": server,
        "paid_provider_requests": 0,
        "operating_cost_usd": None,
        "intervention": "Reuse immutable tool results within one request; unchanged prompt/model/tasks/scorer",
        "quality": "Independent fixed JSON-subset labels, with no labels in runner input",
        "order": "Pilot before held-out freeze; baseline block then frozen findings then candidate and wrong-answer control. Fixed condition order is a confound.",
        "limits": "Owned tiny fixtures, one host, no external users. Offline timings include real execution but are synthetic contract evidence. Model results cover only these bounded fixture agents.",
        "planned_splits": ["pilot", "held_out"],
        "overhead_slots": [
            {"task_id": item["id"], "repetition": rep, "traced": traced}
            for rep in range(2)
            for item in tasks("pilot")
            for traced in ((False, True) if rep % 2 == 0 else (True, False))
        ],
    }
    write_once(root, "protocol.json", protocol)
    command = [sys.executable, "-I", str(HERE / "agent.py"), "--framework", framework]
    if model_url:
        command += ["--model-url", model_url]
    summaries = []
    for split in protocol["planned_splits"]:
        # The held-out manifest is frozen only after pilot reports have been saved.
        workload = {
            "schema_version": "1.0",
            "name": "Guided " + split,
            "repetitions": 1 if split == "pilot" else repetitions,
            "tasks": tasks(split),
        }
        write_once(root, split + "-tasks.json", workload)
        phase = root / split
        capture(
            root / (split + "-tasks.json"),
            phase / "baseline",
            [*command, "--condition", "baseline"],
            timeout=90,
        )
        start(root / (split + "-tasks.json"), phase / "baseline/capture.json", phase / "session")
        for condition in ("candidate", "wrong"):
            capture(
                root / (split + "-tasks.json"),
                phase / condition,
                [*command, "--condition", condition],
                timeout=90,
            )
            report = finish(
                phase / "session/session.json",
                phase / condition / "capture.json",
                phase / (condition + "-results"),
                change=protocol["intervention"]
                if condition == "candidate"
                else "Deliberately incorrect negative control",
            )
            summaries.append(
                {
                    "split": split,
                    "condition": condition,
                    "passed": report["passed"],
                    "passed_attempts": report["passed_attempts"],
                    "planned_attempts": report["planned_attempts"],
                    "report": f"{split}/{condition}-results/report.json",
                }
            )
    controls = []
    for index, slot in enumerate(protocol["overhead_slots"]):
        item = next(item for item in tasks("pilot") if item["id"] == slot["task_id"])
        request = {
            "input": item["input"],
            "trace_path": str(root / f"overhead/{index:04d}-trace.json"),
        }
        write_once(root, f"overhead/{index:04d}-attempt.json", {**slot, "status": "started"})
        try:
            result = execute(
                request,
                condition="baseline",
                model_url=model_url,
                traced=slot["traced"],
                framework=framework,
            )
        except BaseException as error:
            write_once(
                root,
                f"overhead/{index:04d}-receipt.json",
                {
                    **slot,
                    "status": "interrupted",
                    "error": type(error).__name__,
                    "elapsed_ms": None,
                },
            )
            raise
        result.update(
            slot, quality_passed=result["error"] is None and result["output"] == item["expected"]
        )
        write_once(root, f"overhead/{index:04d}-receipt.json", result)
        controls.append(result)
    pairs = []
    for index in range(0, len(controls), 2):
        pair = {item["traced"]: item for item in controls[index : index + 2]}
        comparable = (
            pair[True]["quality_passed"]
            and pair[False]["quality_passed"]
            and pair[True]["output"] == pair[False]["output"]
        )
        pairs.append(
            {
                "task_id": pair[True]["task_id"],
                "repetition": pair[True]["repetition"],
                "difference_ms": pair[True]["elapsed_ms"] - pair[False]["elapsed_ms"]
                if comparable
                else None,
                "comparable": comparable,
                "matching_outputs": pair[True]["output"] == pair[False]["output"],
                "both_correct": pair[True]["quality_passed"] and pair[False]["quality_passed"],
            }
        )
    summary = {
        "schema_version": "1.0",
        "protocol_hash": fingerprint(protocol),
        "comparisons": summaries,
        "overhead": {
            "pairs": pairs,
            "difference_ms": summarize_values([pair["difference_ms"] for pair in pairs]),
            "scope": "Traced minus untraced task wall time with alternating order; includes model variation and context recording, excludes graph setup, exports and analysis. Descriptive, not a causal overhead estimate.",
        },
    }
    write_once(root, "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model-url")
    parser.add_argument("--model-file", type=Path)
    parser.add_argument("--framework", choices=("python", "langgraph"), default="python")
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args()
    try:
        summary = run(
            args.out,
            model_url=args.model_url,
            model_file=args.model_file,
            framework=args.framework,
            repetitions=args.repetitions,
        )
    except (ValueError, FileNotFoundError, importlib.metadata.PackageNotFoundError) as error:
        parser.error(str(error))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
