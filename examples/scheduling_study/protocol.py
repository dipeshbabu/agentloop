"""Freeze independent query batches and a native four-arm ablation."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sqlite3
from contextlib import closing
from pathlib import Path

from agentloop.ablation_protocol import AblationProtocol
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.events import utc_now_iso
from agentloop.scheduling_types import ScheduleConfig
from agentloop.tool_scheduling import scheduling_policy
from examples.real_agent_study.run import file_hash, fingerprint, write_new
from examples.real_harness_study.protocol import DATA_HASHES

ROOT = Path(__file__).resolve().parents[2]
CONDITIONS = (
    "off",
    "trace",
    "shadow_schedule",
    "enforce_schedule",
    "shadow_budget",
    "enforce_budget",
    "shadow_combined",
    "enforce_combined",
)
SCORER_VERSION = "ordered-sql-batch-1.0"
CODE_FILES = (
    "agentloop/scheduling_types.py",
    "agentloop/scheduling_plan.py",
    "agentloop/tool_scheduling.py",
    "agentloop/harness.py",
    "agentloop/harness_usage.py",
    "agentloop/budgets.py",
    "agentloop/budget_types.py",
    "examples/scheduling_study/protocol.py",
    "examples/scheduling_study/runner.py",
)
COLUMNS = (
    "fixed_acidity",
    "volatile_acidity",
    "citric_acid",
    "residual_sugar",
    "chlorides",
    "free_sulfur_dioxide",
    "total_sulfur_dioxide",
    "density",
    "ph",
    "sulphates",
    "alcohol",
    "quality",
)


def configuration():
    return ScheduleConfig("read-only-query-batch", "1.0", "query", max_concurrency=4)


def policies():
    return (scheduling_policy(configuration()), budget_policy(BudgetLimits(max_tool_calls=4)))


def code_hashes():
    return {name: file_hash(ROOT / name) for name in CODE_FILES}


def create_database(path, sources):
    path, sources = Path(path), Path(sources)
    if path.exists():
        raise ValueError("database must be new")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE wines (color TEXT, " + ", ".join(name + " REAL" for name in COLUMNS) + ")"
        )
        for color in ("red", "white"):
            with (sources / f"winequality-{color}.csv").open(newline="") as stream:
                rows = csv.reader(stream, delimiter=";")
                if tuple(value.replace(" ", "_").lower() for value in next(rows)) != COLUMNS:
                    raise ValueError("unexpected public dataset schema")
                connection.executemany(
                    "INSERT INTO wines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ((color, *map(float, row)) for row in rows),
                )
        connection.commit()


def queries(threshold):
    return [
        {
            "id": "red-count",
            "sql": "SELECT count(*) FROM wines WHERE color='red' AND quality>=?",
            "parameters": [threshold],
        },
        {
            "id": "white-alcohol",
            "sql": "SELECT round(avg(alcohol),6) FROM wines WHERE color='white' AND quality>=?",
            "parameters": [threshold],
        },
        {
            "id": "by-color",
            "sql": "SELECT color,round(avg(ph),6),count(*) FROM wines WHERE quality>=? GROUP BY color ORDER BY color",
            "parameters": [threshold],
        },
        {
            "id": "density-range",
            "sql": "SELECT min(density),max(density) FROM wines WHERE quality>=?",
            "parameters": [threshold],
        },
    ]


def read_query(path, query):
    # Every callback opens its own connection; no thread-bound SQLite object is
    # shared. The URI and query_only mode prohibit writes to the frozen dataset.
    with closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")

        def authorize(action, first, second, database, source):
            if (
                action == sqlite3.SQLITE_SELECT
                or (action == sqlite3.SQLITE_READ and first == "wines")
                or (
                    action == sqlite3.SQLITE_FUNCTION
                    and str(second).lower() in {"count", "avg", "round", "min", "max"}
                )
            ):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        result = connection.execute(query["sql"], query["parameters"]).fetchmany(101)
        if len(result) > 100:
            raise ValueError("query result exceeds the declared bound")
        return [list(row) for row in result]


def prepare(root, sources):
    root, sources = Path(root), Path(sources)
    if root.exists() and any(root.iterdir()):
        raise ValueError("study directory must be fresh")
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in DATA_HASHES.items():
        if file_hash(sources / name) != expected:
            raise ValueError("source hash mismatch")
        target = root / "sources" / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((sources / name).read_bytes())
    database = root / "wine.sqlite"
    create_database(database, root / "sources")
    tasks = []
    for index, threshold in enumerate((4, 5, 6, 7, 8, 9)):
        calls = queries(threshold)
        tasks.append(
            {
                "id": f"batch-{index}",
                "split": "pilot" if index < 2 else "held_out",
                "calls": calls,
                "expected": [
                    {"call_id": query["id"], "rows": read_query(database, query)} for query in calls
                ],
            }
        )
    settings = {
        "scheduler": configuration().to_dict(),
        "tool_call_limit": 4,
        "resource": "wine-dataset",
        "access": "read_only",
        "separate_connections": True,
        "reset": "Fresh scheduler, HarnessRun and request list for each observation; each tool opens a read-only connection; OS/filesystem caches remain warm; no artificial sleeps",
    }
    hashes = code_hashes()
    versions = {
        "source": "agentloop:33646da91515bbf0add2f060ee5dbecebc8d75f4+scheduling-file-hashes",
        "model": "none-no-inference",
        "provider": "local-sqlite-" + sqlite3.sqlite_version,
        "configuration": fingerprint(settings),
        "scorer": SCORER_VERSION + ":" + fingerprint(tasks),
        "runner": fingerprint(hashes),
        "environment": fingerprint(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
            }
        ),
        "tools": fingerprint({**DATA_HASHES, "database": file_hash(database)}),
        "reset": fingerprint(settings["reset"]),
    }
    declarations = {
        policy.policy_id: policy.version + ":" + policy.config_hash for policy in policies()
    }
    conditions = {
        "off": {"mode": "uninstrumented", "policies": {}},
        "trace": {"mode": "tracing", "policies": {}},
    }
    for name, selected in (
        ("schedule", policies()[:1]),
        ("budget", policies()[1:]),
        ("combined", policies()),
    ):
        for mode in ("shadow", "enforce"):
            conditions[f"{mode}_{name}"] = {
                "mode": mode,
                "policies": {
                    policy.policy_id: declarations[policy.policy_id] for policy in selected
                },
            }
    schedule = []
    for split in ("pilot", "held_out"):
        subset = [task for task in tasks if task["split"] == split]
        for index, (task, repeat) in enumerate(
            (task, repeat) for task in subset for repeat in ("0", "1")
        ):
            shift = index % len(CONDITIONS)
            schedule.append(
                {
                    "task_id": task["id"],
                    "repetition": repeat,
                    "cache_condition": "warm_os_cache",
                    "order": list(CONDITIONS[shift:] + CONDITIONS[:shift]),
                }
            )
    protocol = AblationProtocol.freeze(
        {
            "schema_version": "1.0",
            "name": "Declared read-only SQL scheduling",
            "workload_id": "wine-readonly-tool-batches-v1",
            "synthetic": False,
            "permission_ref": "Owner delegated public workloads and scorers; UCI Wine Quality CC-BY-4.0; no paid provider",
            "frozen_at": utc_now_iso(),
            "versions": versions,
            "conditions": conditions,
            "tasks": {
                task["id"]: {"split": task["split"], "input_sha256": fingerprint(task)}
                for task in tasks
            },
            "schedule": schedule,
            "quality_gate": {"min_score": 1, "max_regression": 0},
            "bootstrap": {"samples": 1000, "seed": 20260930, "confidence": 0.95},
        }
    )
    plan = {
        "protocol": protocol.to_dict(),
        "tasks": tasks,
        "configuration": settings,
        "code_hashes": hashes,
        "database_sha256": file_hash(database),
        "source_hashes": DATA_HASHES,
        "scorer_version": SCORER_VERSION,
        "limits": "Actual read-only queries on public data, no injected sleep or model inference. Four held-out task batches repeated twice; small local operations may be dominated by scheduler/tracing overhead. OS caching/shared hardware limit causal claims. No production mutation or automatic rollout.",
    }
    write_new(root / "plan.json", plan)
    for name in CODE_FILES:
        target = root / "frozen_code" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    return plan


def load_plan(path):
    path = Path(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    spec = AblationProtocol.from_dict(plan["protocol"]).to_dict()["specification"]
    if (
        plan["code_hashes"] != code_hashes()
        or spec["versions"]["configuration"] != fingerprint(plan["configuration"])
        or spec["versions"]["scorer"] != SCORER_VERSION + ":" + fingerprint(plan["tasks"])
        or file_hash(path.parent / "wine.sqlite") != plan["database_sha256"]
    ):
        raise ValueError("frozen scheduling study inputs changed")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.sources)["protocol"]["protocol_hash"])
