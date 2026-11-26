"""Freeze, derive, evaluate and report the optimization benchmark (issue #406).

    python -m examples.optimization_benchmark.run freeze --sources SRC --out OUT \
        --large-model PATH --small-model PATH
    python -m examples.optimization_benchmark.run derive   --out OUT
    python -m examples.optimization_benchmark.run evaluate --out OUT
    python -m examples.optimization_benchmark.run report   --out OUT

Inference uses two local llama.cpp servers: the large model on 127.0.0.1:8766
and the small one on 127.0.0.1:8767. Every phase refuses to overwrite an
earlier phase's evidence.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agentloop.findings import build_diagnosis
from agentloop.optimization_search import (
    Intervention,
    Objective,
    SearchBudget,
    SearchSpec,
    run_search,
)
from agentloop.pilot_records import METHOD_SCHEMA, compare_methods
from agentloop.tracer import AgentTrace, trace_agent
from examples.optimization_benchmark import agents, scoring
from examples.optimization_benchmark.model import LocalModel

SCHEMA = "agentloop.optimization_benchmark/v1"
SEED = 406
SIZES = {"tuning": 6, "validation": 4, "held_out": 25}
FAMILIES = ("math", "extraction", "coding")
CONTROL = "extraction_single"
URLS = {"large": "http://127.0.0.1:8766", "small": "http://127.0.0.1:8767"}

# Finding type -> configuration change, per agent family. Frozen before any run;
# "*" applies to every family. Types without an entry change nothing.
MAPPING: dict[str, dict[str, dict[str, Any]]] = {
    "cache_context": {"math": {"history": "recent"}},
    "context_relevance": {"math": {"history": "recent"}},
    "parallelize_tools": {"math": {"reuse_calculations": True},
                          "extraction": {"parallel_retrieval": True}},
    "remove_retry_loop": {"math": {"max_parse_retries": 0}, "coding": {"max_repairs": 1}},
    "retry_usefulness": {"math": {"max_parse_retries": 0}, "coding": {"max_repairs": 1}},
    "runaway_loop": {"math": {"max_steps": 4}, "coding": {"max_repairs": 1}},
    "tool_oscillation": {"math": {"max_steps": 4}},
    "semantic_no_progress": {"math": {"max_steps": 4}, "coding": {"max_repairs": 1}},
    "low_contribution": {"math": {"verify_answer": False}, "coding": {"verify_answer": False},
                         "extraction": {"verify_answer": False, "rephrase_retrieval": False}},
    "semantic_redundancy": {"math": {"verify_answer": False}, "coding": {"verify_answer": False},
                            "extraction": {"verify_answer": False, "rephrase_retrieval": False}},
    "route_to_smaller_model": {"*": {"model": "small"}},
}  # fmt: skip
# The manual method: what a developer commonly tries without a profiler.
MANUAL = {"model": "small"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# Tasks -----------------------------------------------------------------------------


def _sample(items: list, count: int, salt: str) -> list:
    rng = random.Random(f"{SEED}:{salt}")  # nosec B311 - reproducible task sampling
    return rng.sample(items, count)


def build_tasks(sources: Path) -> dict[str, list[dict]]:
    """Task inputs and gold answers, sampled with a fixed seed."""
    gsm = [
        json.loads(line)
        for line in (sources / "gsm8k-test.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with gzip.open(sources / "HumanEval.jsonl.gz", "rt", encoding="utf-8") as handle:
        human = [json.loads(line) for line in handle if line.strip()]
    squad = []
    for article in read(sources / "squad-dev-v2.0.json")["data"]:
        for paragraph in article["paragraphs"]:
            for qa in paragraph["qas"]:
                squad.append({"id": qa["id"], "context": paragraph["context"], "question": qa["question"],
                              "gold": [a["text"] for a in qa["answers"]]})  # fmt: skip
    total = sum(SIZES.values())
    tasks = {
        "math": [{"id": f"gsm8k-{i}", "question": item["question"],
                  "gold": scoring.gsm8k_gold(item["answer"])}
                 for i, item in _sample(list(enumerate(gsm)), total, "math")],
        "coding": [{"id": item["task_id"], "prompt": item["prompt"], "entry_point": item["entry_point"],
                    "gold": {"test": item["test"], "entry_point": item["entry_point"]}}
                   for item in _sample(human, total, "coding")],
    }  # fmt: skip
    picked = _sample(squad, total + SIZES["held_out"], "extraction")
    tasks["extraction"] = [{"id": f"squad-{q['id']}", "context": q["context"], "question": q["question"],
                            "gold": q["gold"]} for q in picked[:total]]  # fmt: skip
    tasks[CONTROL] = [{"id": f"squad-{q['id']}", "context": q["context"], "question": q["question"],
                       "gold": q["gold"]} for q in picked[total:]]  # fmt: skip
    return tasks


def splits(family: str, tasks: list[dict]) -> dict[str, list[str]]:
    ids = [t["id"] for t in tasks]
    if family == CONTROL:
        return {"held_out": ids}
    a, b = SIZES["tuning"], SIZES["tuning"] + SIZES["validation"]
    return {"tuning": ids[:a], "validation": ids[a:b], "held_out": ids[b:]}


def visible(task: dict) -> dict:
    return {key: value for key, value in task.items() if key != "gold"}


def score(family: str, output: Any, task: dict) -> float:
    if family == "math":
        return scoring.score_gsm8k(output, task["gold"])
    if family == "coding":
        return scoring.score_humaneval(output, task["gold"])
    return scoring.score_squad(output, task["gold"])


# Running one task --------------------------------------------------------------------


def run_task(family: str, task: dict, config: dict, method: str, out: Path) -> dict:
    settings = {**agents.DEFAULTS[family], **config}
    which = settings.pop("model", "large")
    model = LocalModel(URLS[which], which)
    status, output = "ok", None
    started = time.perf_counter()
    with trace_agent(f"{family}:{task['id']}", metadata={"benchmark_method": method}) as trace:
        try:
            output = agents.AGENTS[family](visible(task), settings, model)
        except Exception as error:  # noqa: BLE001 - a failed run is a result
            status, output = f"error: {type(error).__name__}", None
    wall_ms = (time.perf_counter() - started) * 1000
    quality = score(family, output, task)
    models = [e for e in trace.events if e.event_type == "model_call"]
    row = {
        "family": family, "task": task["id"], "method": method, "model": which, "status": status,
        "quality": quality, "runtime_ms": round(trace.elapsed_ms or wall_ms, 3),
        "model_calls": len(models), "tool_calls": sum(e.event_type == "tool_call" for e in trace.events),
        "tokens": sum((e.input_tokens or 0) + (e.output_tokens or 0) for e in models),
    }  # fmt: skip
    safe = task["id"].replace("/", "_")
    trace.export_json(out / "traces" / method / family / f"{safe}.json")
    return row


# Phases ------------------------------------------------------------------------------


def freeze(sources: Path, out: Path, large: Path, small: Path) -> None:
    tasks = build_tasks(sources)
    write_new(out / "tasks.json", tasks)
    code = Path(__file__).parent
    protocol = {
        "schema": SCHEMA,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED,
        "sizes": SIZES,
        "sources": {p.name: sha256(p) for p in sorted(sources.iterdir()) if p.is_file()},
        "models": {"large": {"file": large.name, "sha256": sha256(large), "url": URLS["large"]},
                   "small": {"file": small.name, "sha256": sha256(small), "url": URLS["small"]}},
        "code": {p.name: sha256(p) for p in sorted(code.glob("*.py"))},
        "splits": {family: splits(family, items) for family, items in tasks.items()},
        "defaults": agents.DEFAULTS,
        "mapping": MAPPING,
        "manual": MANUAL,
        "unseen_application": "coding",
        "tasks_sha256": sha256(out / "tasks.json"),
    }  # fmt: skip
    write_new(out / "protocol.json", protocol)
    print("froze", {f: {k: len(v) for k, v in s.items()} for f, s in protocol["splits"].items()})


def _tasks(out: Path) -> dict[str, dict[str, dict]]:
    return {
        family: {t["id"]: t for t in items} for family, items in read(out / "tasks.json").items()
    }


def _changes(finding_type: str, family: str) -> dict:
    entry = MAPPING.get(finding_type, {})
    return dict(entry.get("*", {})) | dict(entry.get(family, {}))


def derive(out: Path) -> None:
    protocol, tasks = read(out / "protocol.json"), _tasks(out)
    derivation: dict[str, Any] = {"families": {}}
    rows = []
    for family in FAMILIES:
        dev = protocol["splits"][family]["tuning"] + protocol["splits"][family]["validation"]
        top, seen, estimates = Counter(), Counter(), {}
        clock = time.perf_counter()
        for task_id in dev:
            row = run_task(family, tasks[family][task_id], {}, "dev-baseline", out)
            rows.append(row)
            trace = AgentTrace.from_json(
                out / "traces/dev-baseline" / family / f"{task_id.replace('/', '_')}.json"
            )
            findings = build_diagnosis(trace).get("findings", [])
            for rank, finding in enumerate(findings):
                seen[finding["type"]] += 1
                if rank == 0:
                    top[finding["type"]] += 1
                    estimates.setdefault(finding["type"], []).append(
                        finding.get("estimated_latency_savings_ms") or 0.0
                    )
        diagnose_s = time.perf_counter() - clock
        # Heuristic: the existing ranking's most frequent top finding with a mapping.
        heuristic_type = next((t for t, _ in top.most_common() if _changes(t, family)), None)
        heuristic = _changes(heuristic_type, family) if heuristic_type else {}
        # AgentLoop: search over every mapped finding seen on development traces.
        candidates = {t: _changes(t, family) for t in sorted(seen) if _changes(t, family)}
        names = sorted({json.dumps(c, sort_keys=True) for c in candidates.values()})
        interventions = {f"change{i}": json.loads(n) for i, n in enumerate(names)}
        sources = {name: sorted(t for t, c in candidates.items() if json.dumps(c, sort_keys=True) == json.dumps(change, sort_keys=True))
                   for name, change in interventions.items()}  # fmt: skip
        selected, search_report = {}, None
        if interventions:
            conflicts = {
                a: {b for b in interventions if b != a and any(
                    k in interventions[b] and interventions[b][k] != v for k, v in interventions[a].items())}
                for a in interventions
            }  # fmt: skip
            spec = SearchSpec(
                interventions=[
                    Intervention(name, conflicts_with=conflicts[name]) for name in interventions
                ],
                tuning_cases=protocol["splits"][family]["tuning"],
                held_out_cases=protocol["splits"][family]["validation"],
                objectives=[Objective("runtime_ms", "min")],
                quality_floor=0.0,
                budget=SearchBudget(max_attempts=120, max_elapsed_s=6 * 3600),
                seed=SEED,
            )

            def evaluate(combo, case, family=family):
                config = {}
                for name in combo:
                    config |= interventions[name]
                label = "search-" + ("+".join(combo) or "baseline")
                result = run_task(family, tasks[family][case], config, label, out)
                if result["status"] != "ok":
                    return {"status": "error", "reason": result["status"]}
                return {"quality": result["quality"], "runtime_ms": result["runtime_ms"]}

            search_report = run_search(spec, evaluate)
            if search_report["decision"]["status"] == "recommend_for_review":
                for name in search_report["decision"]["recommendation"]:
                    selected |= interventions[name]
        derivation["families"][family] = {
            "top_findings": dict(top), "seen_findings": dict(seen),
            "estimated_latency_savings_ms": {t: statistics.fmean(v) for t, v in estimates.items()},
            "heuristic": {"finding": heuristic_type, "config": heuristic},
            "agentloop": {"interventions": interventions, "from_findings": sources, "config": selected,
                          "decision": search_report["decision"] if search_report else None,
                          "overhead": search_report["overhead"] if search_report else None},
            "diagnosis_and_baseline_s": round(diagnose_s, 3),
        }  # fmt: skip
        if search_report:
            write_new(out / f"search-{family}.json", search_report)
    derivation["methods"] = {
        family: {
            "none": {},
            "heuristic": derivation["families"][family]["heuristic"]["config"],
            "agentloop": derivation["families"][family]["agentloop"]["config"],
            "manual": MANUAL,
        }
        for family in FAMILIES
    }
    # Configuration passes have nothing to change on the one-call control.
    derivation["methods"][CONTROL] = {
        "none": {},
        "heuristic": {},
        "agentloop": {},
        "manual": MANUAL,
    }
    write_new(out / "derivation.json", derivation)
    write_new(out / "dev-baseline.json", rows)
    print(json.dumps(derivation["methods"], indent=1))


def evaluate(out: Path) -> None:
    protocol, tasks = read(out / "protocol.json"), _tasks(out)
    methods = read(out / "derivation.json")["methods"]
    rows = []
    for family in (*FAMILIES, CONTROL):
        order = list(methods[family])
        for index, task_id in enumerate(protocol["splits"][family]["held_out"]):
            rotated = order[index % len(order) :] + order[: index % len(order)]
            for method in rotated:
                rows.append(
                    run_task(family, tasks[family][task_id], methods[family][method], method, out)
                )
        print(family, "done")
    write_new(out / "held-out.json", rows)


def report(out: Path) -> None:
    rows = read(out / "held-out.json")
    derivation = read(out / "derivation.json")
    kinds = {"none": "no_optimization", "heuristic": "heuristic_ranking", "manual": "manual",
             "agentloop": "agentloop_pass"}  # fmt: skip
    method_rows = [
        {"schema": METHOD_SCHEMA, "workload": f"{r['family']}:{r['task']}".replace("/", "_")[:64],
         "method": r["method"], "method_kind": kinds[r["method"]], "split": "held_out",
         "negative_control": r["family"] == CONTROL, "quality_source": "independent",
         "quality": r["quality"], "runtime_ms": r["runtime_ms"], "cost_usd": None}
        for r in rows
    ]  # fmt: skip
    comparison = compare_methods(method_rows)
    summary = {}
    for family in (*FAMILIES, CONTROL):
        summary[family] = {}
        for method in kinds:
            mine = [r for r in rows if r["family"] == family and r["method"] == method]
            if not mine:
                continue
            summary[family][method] = {
                "tasks": len(mine),
                "quality_mean": round(statistics.fmean(r["quality"] for r in mine), 4),
                "errors": sum(r["status"] != "ok" for r in mine),
                "median_runtime_ms": round(statistics.median(r["runtime_ms"] for r in mine), 1),
                "total_runtime_s": round(sum(r["runtime_ms"] for r in mine) / 1000, 1),
                "model_calls": sum(r["model_calls"] for r in mine),
                "tokens": sum(r["tokens"] for r in mine),
            }
    write_new(out / "results.json", {"schema": SCHEMA, "comparison": comparison, "summary": summary,
                                     "derivation": derivation})  # fmt: skip
    print(json.dumps(summary, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="phase", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--sources", type=Path, required=True)
    f.add_argument("--large-model", type=Path, required=True)
    f.add_argument("--small-model", type=Path, required=True)
    for name in ("freeze", "derive", "evaluate", "report"):
        (f if name == "freeze" else sub.add_parser(name)).add_argument(
            "--out", type=Path, required=True
        )
    args = parser.parse_args()
    if args.phase == "freeze":
        freeze(args.sources, args.out, args.large_model, args.small_model)
    else:
        {"derive": derive, "evaluate": evaluate, "report": report}[args.phase](args.out)


if __name__ == "__main__":
    main()
