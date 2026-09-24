"""Single-decision real outcomes with prospectively archived estimator predictions."""

from __future__ import annotations

import argparse
import json
import platform
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from agentloop import reset_runtime
from agentloop.events import utc_now_iso
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates
from agentloop.tracer import AgentTrace, trace_agent
from examples.real_agent_study.api import LocalModel
from examples.real_agent_study.run import MODELS, file_hash, fingerprint, verify_server, write_new

PROMPT = "Solve the math problem. Return only a JSON object with an answer field containing the final number as a plain decimal string, without units or explanation."
SCORER_VERSION = "bounded-numeric-answer-1.0"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 128}},
    "required": ["answer"],
    "additionalProperties": False,
}


def normalize_answer(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value):
        return None
    try:
        number = Decimal(value.replace(",", ""))
        if not number.is_finite() or not -15 <= number.adjusted() <= 15:
            return None
        if number == 0:
            return "0"
        rendered = format(number, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except InvalidOperation:
        return None


def code_hashes():
    hashes = {
        "runner": file_hash(__file__),
        "model_adapter": file_hash(Path(__file__).parent / "real_agent_study/api.py"),
    }
    root = Path(__file__).resolve().parents[1]
    for name in ("rules.py", "estimates.py", "metrics.py", "tracer.py"):
        hashes["agentloop/" + name] = file_hash(root / "agentloop" / name)
    return hashes


def prepare(out, prior_study):
    out, prior_study = Path(out), Path(prior_study)
    if out.exists() and any(out.iterdir()):
        raise ValueError("use a fresh study directory")
    data = prior_study / "sources/gsm8k-test.jsonl"
    if file_hash(data) != "3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14":
        raise ValueError("GSM8K source mismatch")
    prior = json.loads((prior_study / "evaluation-frozen.json").read_text(encoding="utf-8"))
    excluded = {task["source_index"] for task in prior["tasks"] if task["workload"] == "math"}
    problems = [json.loads(line) for line in data.read_text(encoding="utf-8").splitlines()]
    order = sorted(
        (index for index in range(len(problems)) if index not in excluded),
        key=lambda index: fingerprint(["single-decision-calibration-v1", index]),
    )
    tasks, seen = [], set()
    for index in order:
        item = problems[index]
        expected = normalize_answer(item["answer"].rsplit("####", 1)[1].strip())
        if expected is None or item["question"] in seen:
            continue
        seen.add(item["question"])
        tasks.append(
            {
                "id": f"answer-{len(tasks):02d}",
                "split": "fit" if len(tasks) < 2 else "held_out",
                "source_index": index,
                "prompt": item["question"],
                "expected": expected,
            }
        )
        if len(tasks) == 8:
            break
    configuration = {
        "max_output_tokens": 32,
        "timeout_s": 90,
        "temperature": 0,
        "cache_prompt": False,
        "prompt": PROMPT,
        "response_schema": RESPONSE_SCHEMA,
        "repetitions": 2,
        "server": {
            "build": "b10964",
            "ctx_size": 4096,
            "threads": 6,
            "batch_size": 256,
            "ubatch_size": 128,
            "gpu_layers": 99,
            "parallel": 1,
        },
    }
    intervention = {
        "type": "model_routing",
        "configuration": {
            "baseline": MODELS["baseline"],
            "candidate": MODELS["candidate"],
            "scope": "single answer model call",
        },
    }
    protocol = {
        "schema_version": "1.0",
        "core_revision": "0876e143010a753408c6dd032c8ca18e3380091a",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "gpu": "GTX 1650 4GB",
            "driver": "566.36",
            "provider": "llama.cpp b10964 b29c606e2 Vulkan",
        },
        "frozen_at": utc_now_iso(),
        "workload": "GSM8K direct-answer decision v1",
        "models": MODELS,
        "tasks": tasks,
        "configuration": configuration,
        "intervention": intervention,
        "code_hashes": code_hashes(),
        "scorer_version": SCORER_VERSION,
        "source_sha256": file_hash(data),
        "excluded_prior_indices": sorted(excluded),
        "paid_provider_budget_usd": 0,
        "quality_gate": {"min_score": 1, "max_regression": 0},
        "limits": "One decision, not a new autonomous-agent benchmark. Two fit and six held-out tasks, two repetitions; public benchmark contamination and shared-host/order effects possible. All measurable failures enter raw latency diagnostics. Cost remains unknown. Coefficients are diagnostic only and never applied to runtime.",
    }
    write_new(out / "protocol.json", protocol)
    (out / "gsm8k-LICENSE.txt").write_bytes(
        (prior_study / "sources/gsm8k-LICENSE.txt").read_bytes()
    )
    return protocol


def load_protocol(root):
    protocol = json.loads((Path(root) / "protocol.json").read_text(encoding="utf-8"))
    if (
        protocol["code_hashes"] != code_hashes()
        or protocol["models"] != MODELS
        or protocol["configuration"]["prompt"] != PROMPT
    ):
        raise ValueError("frozen implementation mismatch")
    tasks = protocol.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > 1000:
        raise ValueError("invalid task inventory")
    seen = set()
    for task in tasks:
        identity = task.get("id")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"answer-[0-9]{2}", identity)
            or identity in seen
            or task.get("split") not in {"fit", "held_out"}
            or not isinstance(task.get("prompt"), str)
            or not 1 <= len(task["prompt"]) <= 10000
            or normalize_answer(task.get("expected")) != task.get("expected")
            or task.get("expected") is None
        ):
            raise ValueError("invalid or duplicate frozen task")
        seen.add(identity)
    return protocol


def run_condition(root, condition, repetition, base_url, model_file):
    root = Path(root)
    protocol = load_protocol(root)
    reset_runtime()
    server = verify_server(base_url, MODELS[condition], model_file)
    block = root / condition / str(repetition)
    if block.exists():
        raise ValueError("condition slot already exists")
    if condition == "candidate":
        for task in protocol["tasks"]:
            before = root / "baseline" / str(repetition) / task["id"]
            journal = json.loads((before / "journal.json").read_text(encoding="utf-8"))
            prediction = json.loads((before / "prediction.json").read_text(encoding="utf-8"))
            if journal["prediction_hash"] != fingerprint(prediction) or journal[
                "protocol_hash"
            ] != fingerprint(protocol):
                raise ValueError("candidate requires original archived predictions")
    write_new(
        block / "schedule.json",
        {
            "protocol_hash": fingerprint(protocol),
            "condition": condition,
            "repetition": repetition,
            "task_ids": [task["id"] for task in protocol["tasks"]],
            "server": server,
        },
    )
    for task in protocol["tasks"]:
        folder = block / task["id"]
        write_new(
            folder / "attempt.json",
            {
                "task_id": task["id"],
                "repetition": repetition,
                "condition": condition,
                "protocol_hash": fingerprint(protocol),
                "started_at": utc_now_iso(),
            },
        )
        output, status, error = None, "completed", None
        with trace_agent(
            "single-answer-calibration",
            metadata={
                "synthetic": False,
                "workload": protocol["workload"],
                "task_id": task["id"],
                "repetition": repetition,
                "protocol_hash": fingerprint(protocol),
                "condition": condition,
            },
        ) as trace:
            model = LocalModel(
                base_url,
                MODELS[condition],
                max_tokens=32,
                timeout_s=90,
                seed=20260923 + repetition,
                trace=trace,
            )
            model.response_schema = RESPONSE_SCHEMA
            try:
                text = model.complete(
                    [
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": task["prompt"]},
                    ],
                    remaining_s=90,
                )
                raw = json.loads(text)
                output = normalize_answer(raw.get("answer")) if isinstance(raw, dict) else None
            except Exception as exc:
                status, error = "failed", type(exc).__name__
        score = float(output is not None and output == task["expected"] and status == "completed")
        trace.metadata.update(
            output=output, success=bool(score), quality_score=score, task_status=status
        )
        trace.export_json(folder / "trace.json")
        write_new(
            folder / "receipt.json",
            {
                "task_id": task["id"],
                "condition": condition,
                "repetition": repetition,
                "status": status,
                "error_category": error,
                "output": output,
                "quality_score": score,
                "trace_hash": fingerprint(trace.to_dict()),
                "calls": model.receipts,
                "paid_provider_spend_usd": 0,
                "operating_cost_usd": None,
            },
        )
        if condition == "baseline":
            diagnosis = build_diagnosis(trace)
            predictions = [
                item for item in diagnosis["findings"] if item["type"] == "route_to_smaller_model"
            ]
            write_new(folder / "diagnosis.json", diagnosis)
            if len(predictions) != 1:
                raise ValueError(
                    "single-decision cohort requires exactly one original routing prediction; raw attempt retained"
                )
            write_new(folder / "prediction.json", predictions[0])
            write_new(
                folder / "journal.json",
                {
                    "protocol_hash": fingerprint(protocol),
                    "prediction_hash": fingerprint(predictions[0]),
                    "prediction_recorded_at": utc_now_iso(),
                },
            )
        else:
            before = root / "baseline" / str(repetition) / task["id"]
            baseline = AgentTrace.from_json(before / "trace.json")
            diagnosis = json.loads((before / "diagnosis.json").read_text(encoding="utf-8"))
            prediction = json.loads((before / "prediction.json").read_text(encoding="utf-8"))
            quality = build_quality_report(
                [
                    {
                        "id": task["id"],
                        "expected": task["expected"],
                        "baseline_output": baseline.metadata["output"],
                        "candidate_output": output,
                    }
                ],
                min_score=1,
            )
            record = build_intervention(
                baseline,
                trace,
                target_finding_ids=[prediction["finding_id"]],
                intervention_type=protocol["intervention"]["type"],
                configuration=protocol["intervention"]["configuration"],
                metadata={
                    "synthetic": False,
                    "protocol_hash": fingerprint(protocol),
                    "task_id": task["id"],
                    "repetition": repetition,
                },
                diagnosis=diagnosis,
                gates=ReplayGates(min_quality_score=1),
                quality_report=quality,
            ).to_dict()
            write_new(folder / "intervention.json", record)
            write_new(
                folder / "journal.json",
                {"record_hash": fingerprint(record), "outcome_recorded_at": utc_now_iso()},
            )
        print(
            json.dumps(
                {
                    "task": task["id"],
                    "split": task["split"],
                    "condition": condition,
                    "repetition": repetition,
                    "status": status,
                    "quality": score,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--prior-study", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--condition", choices=MODELS, required=True)
    run.add_argument("--repetition", type=int, choices=(0, 1), required=True)
    run.add_argument("--base-url", default="http://127.0.0.1:8766")
    run.add_argument("--model-file", type=Path, required=True)
    run.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(fingerprint(prepare(args.out, args.prior_study)))
    elif not args.execute:
        parser.error("real inference requires --execute")
    else:
        run_condition(args.root, args.condition, args.repetition, args.base_url, args.model_file)
