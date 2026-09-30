"""Freeze context interventions before evaluating any task output."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from agentloop.ablation_protocol import AblationProtocol
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.context_controls import context_policy
from agentloop.context_types import ContextPolicyConfig
from agentloop.events import utc_now_iso
from examples.real_agent_study.run import MODELS, file_hash, fingerprint, write_new
from examples.real_agent_study.scoring import SCORER_VERSION
from examples.real_agent_study.tools import WineDatabase
from examples.real_harness_study.protocol import DATA_HASHES

ROOT = Path(__file__).resolve().parents[2]
CONDITIONS = (
    "off",
    "trace",
    "shadow_context",
    "enforce_context",
    "shadow_budget",
    "enforce_budget",
    "shadow_combined",
    "enforce_combined",
)
CODE_FILES = (
    "examples/context_study/protocol.py",
    "examples/context_study/runner.py",
    "examples/real_agent_study/api.py",
    "examples/real_agent_study/scoring.py",
    "examples/real_agent_study/tools.py",
    "agentloop/context_controls.py",
    "agentloop/context_types.py",
    "agentloop/harness.py",
    "agentloop/budgets.py",
    "agentloop/budget_types.py",
)
BACKGROUND_SQL = "SELECT color,quality,alcohol,ph,sulphates FROM wines LIMIT 24"


def configuration():
    return ContextPolicyConfig(
        "optional-background-summary",
        "1.0",
        "answer",
        SCORER_VERSION,
        max_summary_chars=480,
        on_invalid="error",
    )


def policies():
    return (
        context_policy(configuration()),
        budget_policy(BudgetLimits(max_model_calls=2, max_tokens=18000)),
    )


def task_definitions():
    return (
        (
            "Give the maximum alcohol of red wine samples.",
            "SELECT max(alcohol) FROM wines WHERE color='red'",
        ),
        (
            "Count white wines with residual sugar greater than 10.",
            "SELECT count(*) FROM wines WHERE color='white' AND residual_sugar>10",
        ),
        (
            "Give average pH for quality-7 red wines, rounded to 3 decimals.",
            "SELECT round(avg(ph),3) FROM wines WHERE color='red' AND quality=7",
        ),
        (
            "Give color and maximum total sulfur dioxide, ordered by color.",
            "SELECT color,max(total_sulfur_dioxide) FROM wines GROUP BY color ORDER BY color",
        ),
        (
            "For white wines with quality at least 7, give quality and count, ordered by quality.",
            "SELECT quality,count(*) FROM wines WHERE color='white' AND quality>=7 GROUP BY quality ORDER BY quality",
        ),
    )


def code_hashes():
    return {name: file_hash(ROOT / name) for name in CODE_FILES}


def prepare(root, sources):
    root, sources = Path(root), Path(sources)
    if root.exists() and any(root.iterdir()):
        raise ValueError("study root must be fresh; retain previous attempts")
    for name, expected in DATA_HASHES.items():
        if file_hash(sources / name) != expected:
            raise ValueError("public source checksum mismatch")
    database = WineDatabase(*((sources / name).read_text() for name in DATA_HASHES))
    try:
        tasks = [
            {
                "id": f"context-{index}",
                "workload": "sql",
                "split": "pilot" if index == 0 else "held_out",
                "prompt": prompt,
                "sql": sql,
                "expected": database.query(sql)["rows"],
            }
            for index, (prompt, sql) in enumerate(task_definitions())
        ]
    finally:
        database.close()
    configuration_snapshot = {
        "context": configuration().to_dict(),
        "background_sql": BACKGROUND_SQL,
        "max_output_tokens": 192,
        "request_timeout_s": 90,
        "cache_prompt": False,
        "summary_model": MODELS["baseline"],
        "main_model": MODELS["baseline"],
        "budget_model_calls": 2,
        "budget_tokens": 18000,
        "reservation_tokens_per_call": 4288,
        "reset": "Fresh SQLite, request, model receipt collector and HarnessRun per observation; provider prompt cache disabled; one readiness warmup before schedule",
    }
    hashes = code_hashes()
    environment = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "gpu": "GTX 1650 4GB",
        "driver": "566.36",
        "paid_provider_budget_usd": 0,
    }
    versions = {
        "source": "agentloop:320b47e6a72857359e556d127f749503a466f3ea+context-file-hashes",
        "model": fingerprint(MODELS["baseline"]),
        "provider": "llama.cpp:b10964:b29c606e2:Vulkan",
        "configuration": fingerprint(configuration_snapshot),
        "scorer": SCORER_VERSION + ":" + fingerprint(tasks),
        "runner": fingerprint(hashes),
        "environment": fingerprint(environment),
        "tools": fingerprint(DATA_HASHES),
        "reset": fingerprint(configuration_snapshot["reset"]),
    }
    declarations = {p.policy_id: p.version + ":" + p.config_hash for p in policies()}
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
    }
    for group, selected in (
        ("context", policies()[:1]),
        ("budget", policies()[1:]),
        ("combined", policies()),
    ):
        for mode in ("shadow", "enforce"):
            conditions[f"{mode}_{group}"] = {
                "mode": mode,
                "policies": {p.policy_id: declarations[p.policy_id] for p in selected},
            }
    schedule = []
    for split in ("pilot", "held_out"):
        split_tasks = [task for task in tasks if task["split"] == split]
        for index, (task, repetition) in enumerate(
            (task, repetition) for task in split_tasks for repetition in ("0", "1")
        ):
            shift = index % len(CONDITIONS)
            schedule.append(
                {
                    "task_id": task["id"],
                    "repetition": repetition,
                    "cache_condition": "prompt_cache_disabled",
                    "order": list(CONDITIONS[shift:] + CONDITIONS[:shift]),
                }
            )
    protocol = AblationProtocol.freeze(
        {
            "schema_version": "1.0",
            "name": "Local model: explicit optional-tool summary",
            "workload_id": "wine-context-summary-v1",
            "synthetic": False,
            "permission_ref": "Owner delegated public workloads and scorers; UCI Wine Quality CC-BY-4.0; USD0 paid-provider spend",
            "frozen_at": utc_now_iso(),
            "versions": versions,
            "conditions": conditions,
            "tasks": {
                task["id"]: {"split": task["split"], "input_sha256": fingerprint(task)}
                for task in tasks
            },
            "schedule": schedule,
            "quality_gate": {"min_score": 1, "max_regression": 0},
            "bootstrap": {"samples": 1000, "seed": 20260924, "confidence": 0.95},
        }
    )
    plan = {
        "protocol": protocol.to_dict(),
        "tasks": tasks,
        "code_hashes": hashes,
        "configuration": configuration_snapshot,
        "environment": environment,
        "model": MODELS["baseline"],
        "source_hashes": DATA_HASHES,
        "limits": "A host-prepared SQL answer stage, not autonomous query planning. Caller knows older sample rows are optional and protects current query results. Four held-out tasks repeated twice; exploratory, no population guarantee. Count all summaries and failures. No automatic promotion.",
    }
    write_new(root / "plan.json", plan)
    for name in DATA_HASHES:
        target = root / "sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((sources / name).read_bytes())
    for name in CODE_FILES:
        target = root / "frozen_code" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    return plan


def load_plan(path):
    path = Path(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    protocol = AblationProtocol.from_dict(plan["protocol"])
    versions = protocol.to_dict()["specification"]["versions"]
    if (
        plan["code_hashes"] != code_hashes()
        or versions["configuration"] != fingerprint(plan["configuration"])
        or versions["scorer"] != SCORER_VERSION + ":" + fingerprint(plan["tasks"])
        or plan["model"] != MODELS["baseline"]
    ):
        raise ValueError("frozen study inputs changed")
    for name, expected in DATA_HASHES.items():
        if file_hash(path.parent / "sources" / name) != expected:
            raise ValueError("frozen public data changed")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.sources)["protocol"]["protocol_hash"])
