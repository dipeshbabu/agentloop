"""Freeze new public tasks and one manually configured model route."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from agentloop.events import utc_now_iso
from agentloop.routing_types import RoutingConfig
from examples.real_agent_study.run import MODELS, file_hash, fingerprint, write_new
from examples.real_calibration_study import SCORER_VERSION, normalize_answer

from .runtime import identity

ROOT = Path(__file__).resolve().parents[2]
PROMPT = "Solve the math problem. Return only a JSON object with an answer field containing the final number as a plain decimal string, without units or explanation."
DATA_SHA = "3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14"
CODE_FILES = (
    "agentloop/model_routing.py",
    "agentloop/routing_types.py",
    "agentloop/routing_checks.py",
    "agentloop/harness.py",
    "agentloop/harness_usage.py",
    "agentloop/budgets.py",
    "agentloop/budget_types.py",
    "agentloop/context_types.py",
    "examples/routing_study/runtime.py",
    "examples/routing_study/protocol.py",
    "examples/routing_study/runner.py",
    "examples/real_calibration_study.py",
)


def configuration(condition):
    return RoutingConfig(
        "reviewed-numeric-route",
        "1.0",
        "answer",
        SCORER_VERSION,
        identity("baseline"),
        identity(condition),
        (identity("baseline"), identity("candidate")),
    )


def code_hashes():
    return {name: file_hash(ROOT / name) for name in CODE_FILES}


def prepare(root, data, prior_agent, prior_calibration):
    root, data = Path(root), Path(data)
    if root.exists() and any(root.iterdir()):
        raise ValueError("study directory must be fresh")
    if file_hash(data) != DATA_SHA:
        raise ValueError("public dataset checksum mismatch")
    excluded, prior = set(), []
    for path in (Path(prior_agent), Path(prior_calibration)):
        value = json.loads(path.read_text(encoding="utf-8"))
        indices = [task["source_index"] for task in value["tasks"] if "source_index" in task]
        excluded.update(indices)
        prior.append({"file": path.name, "sha256": file_hash(path), "indices": sorted(indices)})
    corpus = [json.loads(line) for line in data.read_text(encoding="utf-8").splitlines()]
    order = sorted(
        (index for index in range(len(corpus)) if index not in excluded),
        key=lambda index: fingerprint(["explicit-model-route-191-v1", index]),
    )
    tasks, seen = [], set()
    for index in order:
        item = corpus[index]
        expected = normalize_answer(item["answer"].rsplit("####", 1)[1].strip())
        if expected is None or item["question"] in seen:
            continue
        seen.add(item["question"])
        tasks.append(
            {
                "id": f"route-{len(tasks):02d}",
                "split": "pilot" if len(tasks) < 2 else "held_out",
                "source_index": index,
                "prompt": item["question"],
                "expected": expected,
            }
        )
        if len(tasks) == 8:
            break
    plan = {
        "schema_version": "1.0",
        "frozen_at": utc_now_iso(),
        "core_revision": "bb027c2824cdd5a43ffbb95ecfac5d09df2e61b7+routing-code-hashes",
        "models": MODELS,
        "tasks": tasks,
        "source_sha256": DATA_SHA,
        "exclusions": prior,
        "scorer_version": SCORER_VERSION,
        "code_hashes": code_hashes(),
        "configuration": {
            "prompt": PROMPT,
            "max_tokens": 64,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "cache_prompt": False,
            "repetitions": 2,
            "model_call_limit": 1,
            "token_limit": 4160,
            "routes": {condition: configuration(condition).to_dict() for condition in MODELS},
            "condition_order": ["baseline", "candidate"],
            "server": {
                "build": "b10964",
                "context_window": 4096,
                "threads": 6,
                "batch_size": 256,
                "ubatch_size": 128,
                "parallel": 1,
                "gpu_layers": 99,
            },
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "gpu": "GTX 1650 4GB",
            "driver": "566.36",
        },
        "paid_provider_budget_usd": 0,
        "quality_acceptance": "Independent exact bounded numeric answer; all planned held-out tasks retained; no decrease from baseline, no route promotion.",
        "limits": "Public benchmark, not production. New task indices exclude #181/#189, but model training overlap remains possible. Fixed baseline-before-candidate blocks preserve original predictions and leave time/order confounding. Six held-out tasks, two repetitions; exploratory task-weighted intervals. Unknown operating cost, no paid provider.",
    }
    plan["plan_hash"] = fingerprint(plan)
    write_new(root / "plan.json", plan)
    (root / "sources").mkdir()
    (root / "sources/gsm8k-test.jsonl").write_bytes(data.read_bytes())
    for name in CODE_FILES:
        path = root / "frozen_code" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    return plan


def load_plan(path):
    path = Path(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if (
        fingerprint(unsigned) != plan["plan_hash"]
        or plan["code_hashes"] != code_hashes()
        or plan["models"] != MODELS
        or plan["scorer_version"] != SCORER_VERSION
        or file_hash(path.parent / "sources/gsm8k-test.jsonl") != DATA_SHA
    ):
        raise ValueError("frozen routing study inputs changed")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--prior-agent", type=Path, required=True)
    parser.add_argument("--prior-calibration", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.data, args.prior_agent, args.prior_calibration)["plan_hash"])
