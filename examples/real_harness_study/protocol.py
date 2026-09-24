"""Freeze new SQL tasks and counterbalanced single-policy comparisons."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_harness_study"

from agentloop.ablation_protocol import AblationProtocol
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.events import utc_now_iso

try:
    from real_agent_study.run import MODELS, file_hash, fingerprint, write_new
    from real_agent_study.scoring import SCORER_VERSION
    from real_agent_study.tools import WineDatabase
except ModuleNotFoundError:
    from examples.real_agent_study.run import MODELS, file_hash, fingerprint, write_new
    from examples.real_agent_study.scoring import SCORER_VERSION
    from examples.real_agent_study.tools import WineDatabase

SOURCE_REVISION = "009c8a475265c41491e47aed002a3d67597e9723"
DATA_HASHES = {
    "winequality-red.csv": "4a402cf041b025d4566d954c3b9ba8635a3a8a01e039005d97d6a710278cf05e",
    "winequality-white.csv": "76c3f809815c17c07212622f776311faeb31e87610d52c26d87d6e361b169836",
}
CONDITIONS = ("off", "trace", "shadow_2", "enforce_2", "shadow_1", "enforce_1")
ROOT = Path(__file__).resolve().parents[2]
CODE_FILES = (
    "examples/real_harness_study/protocol.py",
    "examples/real_harness_study/runner.py",
    "examples/real_agent_study/agent.py",
    "examples/real_agent_study/api.py",
    "examples/real_agent_study/tools.py",
    "examples/real_agent_study/scoring.py",
    "agentloop/harness.py",
    "agentloop/harness_evidence.py",
    "agentloop/budgets.py",
    "agentloop/budget_types.py",
)


def code_hashes():
    return {name: file_hash(ROOT / name) for name in CODE_FILES}


def policy(limit):
    return budget_policy(
        BudgetLimits(max_model_calls=limit), boundaries=("model",), policy_id="model-call-cap"
    )


def task_definitions():
    return [
        (
            "Count white wine samples with quality at least 7. Return a single count row.",
            "SELECT count(*) FROM wines WHERE color='white' AND quality>=7",
        ),
        (
            "Give average sulphates for red wines rounded to 3 decimals. Return one row.",
            "SELECT round(avg(sulphates),3) FROM wines WHERE color='red'",
        ),
        (
            "Give the minimum alcohol for white wines. Return one row.",
            "SELECT min(alcohol) FROM wines WHERE color='white'",
        ),
        (
            "Count red wine samples with alcohol greater than 10. Return one row.",
            "SELECT count(*) FROM wines WHERE color='red' AND alcohol>10",
        ),
        (
            "Give maximum chlorides among white wines with quality at most 5. Return one row.",
            "SELECT max(chlorides) FROM wines WHERE color='white' AND quality<=5",
        ),
        (
            "For each color, give color and minimum density, ordered by color ascending.",
            "SELECT color,min(density) FROM wines GROUP BY color ORDER BY color",
        ),
        (
            "Give the average free sulfur dioxide for red wines rounded to 3 decimals. Return one row.",
            "SELECT round(avg(free_sulfur_dioxide),3) FROM wines WHERE color='red'",
        ),
        (
            "For samples with alcohol greater than 12, count samples by quality. Return quality and count, ordered by quality ascending.",
            "SELECT quality,count(*) FROM wines WHERE alcohol>12 GROUP BY quality ORDER BY quality",
        ),
        (
            "For quality-6 samples, give color and average fixed acidity rounded to 3 decimals, ordered by color ascending.",
            "SELECT color,round(avg(fixed_acidity),3) FROM wines WHERE quality=6 GROUP BY color ORDER BY color",
        ),
    ]


def prepare(root, sources):
    root, sources = Path(root), Path(sources)
    if root.exists() and any(root.iterdir()):
        raise ValueError("ablation root must be fresh or empty")
    data = {}
    for name, expected in DATA_HASHES.items():
        if file_hash(sources / name) != expected:
            raise ValueError("frozen data mismatch")
        data[name] = (sources / name).read_text(encoding="utf-8")
    database = WineDatabase(data["winequality-red.csv"], data["winequality-white.csv"])
    try:
        tasks = [
            {
                "id": f"wine-{index:02d}",
                "workload": "sql",
                "split": "pilot" if index < 3 else "held_out",
                "prompt": prompt,
                "expected": database.query(sql)["rows"],
                "gold_sql": sql,
            }
            for index, (prompt, sql) in enumerate(task_definitions())
        ]
    finally:
        database.close()
    hashes = code_hashes()
    configuration = {
        "max_model_calls": 4,
        "max_output_tokens": 192,
        "task_timeout_s": 180,
        "request_timeout_s": 90,
        "temperature": 0,
        "cache_prompt": False,
        "ctx_size": 4096,
        "threads": 6,
        "batch_size": 256,
        "ubatch_size": 128,
        "gpu_layers": 99,
        "parallel": 1,
        "count_caps": [2, 1],
        "negative_control": "cap1 deliberately prevents the usual tool-result-to-final-answer turn",
        "reset": "new read-only in-memory SQLite database, messages, model receipt collector and HarnessRun per condition; prompt KV reuse disabled; one fixed model-readiness warmup before the schedule",
    }
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
    }
    for limit in (2, 1):
        declaration = policy(limit)
        for mode in ("shadow", "enforce"):
            conditions[f"{mode}_{limit}"] = {
                "mode": mode,
                "policies": {
                    declaration.policy_id: f"{declaration.version}:{declaration.config_hash}"
                },
            }
    schedule = []
    for split in ("pilot", "held_out"):
        split_tasks = sorted(
            (task for task in tasks if task["split"] == split),
            key=lambda task: fingerprint(task["id"]),
        )
        for index, (task, repetition) in enumerate(
            (task, repetition) for task in split_tasks for repetition in ("0", "1")
        ):
            shift = index % len(CONDITIONS)
            order = list(CONDITIONS[shift:] + CONDITIONS[:shift])
            schedule.append(
                {
                    "task_id": task["id"],
                    "repetition": repetition,
                    "cache_condition": "prompt_cache_disabled",
                    "order": order,
                }
            )
    environment = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "gpu": "GTX 1650 4GB",
        "driver": "566.36",
        "provider_spend_limit_usd": 0,
    }
    versions = {
        "source": "agentloop:" + SOURCE_REVISION,
        "model": fingerprint(MODELS["baseline"]),
        "provider": "llama.cpp:b10964:b29c606e2:Vulkan",
        "configuration": fingerprint(configuration),
        "scorer": SCORER_VERSION
        + ":"
        + fingerprint({"code": hashes["examples/real_agent_study/scoring.py"], "labels": tasks}),
        "runner": fingerprint(hashes),
        "environment": fingerprint(environment),
        "tools": fingerprint(
            {"code": hashes["examples/real_agent_study/tools.py"], "data": DATA_HASHES}
        ),
        "reset": fingerprint(configuration["reset"]),
    }
    spec = {
        "schema_version": "1.0",
        "name": "Real SQL agent: tracing and count-policy ablations",
        "workload_id": "wine-sql-agent-v2",
        "permission_ref": "repository-owner delegated public tasks/scorers; UCI Wine Quality CC-BY-4.0; paid-provider budget USD0",
        "frozen_at": utc_now_iso(),
        "synthetic": False,
        "versions": versions,
        "tasks": {
            task["id"]: {
                "split": task["split"],
                "input_sha256": fingerprint(
                    {"prompt": task["prompt"], "data": DATA_HASHES, "constraints": configuration}
                ),
            }
            for task in tasks
        },
        "conditions": conditions,
        "schedule": schedule,
        "quality_gate": {"min_score": 1, "max_regression": 0},
        "bootstrap": {"samples": 1000, "seed": 20260923, "confidence": 0.95},
    }
    protocol = AblationProtocol.freeze(spec)
    root.mkdir(parents=True, exist_ok=True)
    (root / "sources").mkdir()
    for name, value in data.items():
        (root / "sources" / name).write_bytes((sources / name).read_bytes())
    plan = {
        "schema_version": "1.0",
        "protocol": protocol.to_dict(),
        "tasks": tasks,
        "configuration": configuration,
        "environment": environment,
        "model": MODELS["baseline"],
        "code_hashes": hashes,
        "source_hashes": DATA_HASHES,
        "limits": "New tasks not used in #181. Three pilot and six held-out tasks, two repetitions, balanced condition positions. Public data and shared hardware; exploratory, not a population or causal guarantee. One-call cap is a negative control. No policy combination or automatic promotion.",
    }
    write_new(root / "plan.json", plan)
    return plan


def load_plan(path):
    path = Path(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    protocol = AblationProtocol.from_dict(plan["protocol"])
    if (
        plan["code_hashes"] != code_hashes()
        or plan["model"] != MODELS["baseline"]
        or fingerprint(plan["configuration"])
        != protocol.to_dict()["specification"]["versions"]["configuration"]
    ):
        raise ValueError("frozen implementation/configuration mismatch")
    if len({task["id"] for task in plan["tasks"]}) != len(plan["tasks"]):
        raise ValueError("duplicate task IDs")
    for name, expected in DATA_HASHES.items():
        if file_hash(path.parent / "sources" / name) != expected:
            raise ValueError("frozen source mismatch")
    labels = fingerprint(
        {
            "code": plan["code_hashes"]["examples/real_agent_study/scoring.py"],
            "labels": plan["tasks"],
        }
    )
    if protocol.to_dict()["specification"]["versions"]["scorer"] != SCORER_VERSION + ":" + labels:
        raise ValueError("frozen label mismatch")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.out, args.sources)
    print(result["protocol"]["protocol_hash"])
