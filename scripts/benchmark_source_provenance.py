"""Measure source-mapping precision and capture overhead for issue #395.

Builds a throwaway repository in which every module defines the same function
and method names, traces three calls to one known definition per trial, and
asks the dry-run patch planner where to look. It compares:

* name matching (the behaviour without source receipts): of the candidate files,
  how many contain the traced definition, and is the first candidate right?
* source receipts: of the rewrite-eligible candidates, how many are right?

It also times a decorated call with and without capture, and the one-time cost
of building a receipt at decoration time. Results are printed as JSON; nothing
outside the temporary directory is written.

    uv run python scripts/benchmark_source_provenance.py --modules 20 --calls 20000
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from agentloop.decorators import traceable
from agentloop.patches import build_patch_plan
from agentloop.source_provenance import capture_source
from agentloop.tracer import trace_agent

MODULE = """\
from agentloop.decorators import traceable

CAPTURE = {capture}


class Client:
    @traceable(name="search", capture_source=CAPTURE)
    def search(self, query):
        return query


@traceable(name="search", capture_source=CAPTURE)
def search(query):
    return query
"""


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _trial(root: Path, module, target: str, use_method: bool) -> dict[str, object]:
    with trace_agent("benchmark") as trace:
        for _ in range(3):
            module.Client().search("q") if use_method else module.search("q")
    for event in trace.events:
        event.metadata["parallel_safe"] = True
    plan = build_patch_plan(trace, repo_path=root, allowed_root=root.parent)
    items = [item for item in plan["patch_plans"] if item["type"] == "parallelize_tools"]
    files = items[0]["files"] if items else []
    eligible = [item for item in files if item["rewrite_eligible"]]
    return {
        "candidates": len(files),
        "correct_candidates": sum(item["path"] == target for item in files),
        "first_correct": bool(files) and files[0]["path"] == target,
        "eligible": len(eligible),
        "eligible_correct": sum(item["path"] == target for item in eligible),
    }


def _mapping(modules: int, capture: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        (root / ".git").mkdir(parents=True)
        loaded = []
        for number in range(modules):
            path = root / f"agents/agent_{number:03d}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(MODULE.format(capture=capture), encoding="utf-8")
            loaded.append(
                (path.relative_to(root).as_posix(), _load(path, f"bench_{capture}_{number}"))
            )
        trials = [
            _trial(root, module, target, use_method)
            for target, module in loaded
            for use_method in (False, True)
        ]
    candidates = sum(t["candidates"] for t in trials)
    eligible = sum(t["eligible"] for t in trials)
    return {
        "trials": len(trials),
        "candidate_files": candidates,
        "candidate_precision": _ratio(sum(t["correct_candidates"] for t in trials), candidates),
        "first_candidate_accuracy": _ratio(sum(t["first_correct"] for t in trials), len(trials)),
        "eligible_candidates": eligible,
        "eligible_precision": _ratio(sum(t["eligible_correct"] for t in trials), eligible),
    }


def _ratio(numerator: int, denominator: int) -> dict[str, object]:
    return {
        "count": numerator,
        "of": denominator,
        "value": None if denominator == 0 else round(numerator / denominator, 4),
    }


def _per_call_ns(func, calls: int, repeats: int) -> float:
    samples = []
    with trace_agent("overhead"):
        for _ in range(repeats):
            start = time.perf_counter_ns()
            for _ in range(calls):
                func(1)
            samples.append((time.perf_counter_ns() - start) / calls)
    return statistics.median(samples)


def _overhead(calls: int, repeats: int) -> dict[str, object]:
    def body(value):
        return value

    plain = traceable(name="plain", capture_source=False)(body)
    captured = traceable(name="captured", capture_source=True)(body)
    plain_ns = _per_call_ns(plain, calls, repeats)
    captured_ns = _per_call_ns(captured, calls, repeats)
    start = time.perf_counter_ns()
    for _ in range(200):
        capture_source(_overhead)
    return {
        "calls_per_sample": calls,
        "samples": repeats,
        "per_call_ns_without_capture": round(plain_ns),
        "per_call_ns_with_capture": round(captured_ns),
        "per_call_overhead_ns": round(captured_ns - plain_ns),
        "receipt_build_us_once_per_function": round(
            (time.perf_counter_ns() - start) / 200 / 1000, 1
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--modules", type=int, default=20)
    parser.add_argument("--calls", type=int, default=20000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.modules < 1 or args.calls < 1 or args.repeats < 1:
        parser.error("--modules, --calls and --repeats must be positive")
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "modules": args.modules,
        "name_matching": _mapping(args.modules, capture=False),
        "source_receipts": _mapping(args.modules, capture=True),
        "overhead": _overhead(args.calls, args.repeats),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
