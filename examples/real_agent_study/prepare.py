"""Prepare public task sources and independently labeled pilot/evaluation tasks."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

from .scoring import SCORER_VERSION
from .tools import WineDatabase

GSM_REVISION = "3101c7d5072418e28b9008a6636bde82a006892c"
REPOSITORY_REVISION = "7eaeaa2a435c59f3f73e6dedd6b173d85b4d2d11"
SOURCES = {
    "wine-quality.zip": (
        "https://archive.ics.uci.edu/static/public/186/wine+quality.zip",
        "3ed56667f4b828242bd732d7d1dd7f2861e54432239d7fa63877014cbb0304d4",
    ),
    "gsm8k-test.jsonl": (
        f"https://raw.githubusercontent.com/openai/grade-school-math/{GSM_REVISION}/grade_school_math/data/test.jsonl",
        "3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14",
    ),
    "gsm8k-LICENSE.txt": (
        f"https://raw.githubusercontent.com/openai/grade-school-math/{GSM_REVISION}/LICENSE",
        "86bbb73e855821d7c401912fd4bf82e34313e6e3b6fd6f909f2b6cc9e209a53b",
    ),
}
REPOSITORY_FILES = (
    "LICENSE",
    "agentloop/tracer.py",
    "agentloop/parallelism.py",
    "agentloop/costs.py",
    "agentloop/quality.py",
    "agentloop/replay.py",
    "agentloop/ci.py",
    "agentloop/interventions.py",
    "agentloop/studies.py",
    "agentloop/html_report.py",
    "agentloop/tokens.py",
)
REPOSITORY_HASH = "26eed6a6822be5376e1f42dde082004d3178543dd66b9612fc6cca4b76978854"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def download(url):
    with urllib.request.urlopen(url, timeout=60) as response:  # nosec B310 - fixed HTTPS sources above
        value = response.read(20_000_001)
    if len(value) > 20_000_000:
        raise ValueError("source exceeds 20 MB")
    return value


def prepare_sources(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (url, expected) in SOURCES.items():
        path = folder / name
        value = path.read_bytes() if path.exists() else download(url)
        if digest(value) != expected:
            raise ValueError(f"source checksum mismatch: {name}")
        if not path.exists():
            path.write_bytes(value)
        manifest[name] = {"url": url, "sha256": expected, "bytes": len(value)}
    with zipfile.ZipFile(folder / "wine-quality.zip") as archive:
        for name in ("winequality-red.csv", "winequality-white.csv", "winequality.names"):
            # Fixed member names; no path extraction from an untrusted archive.
            value = archive.read(name)
            (folder / name).write_bytes(value)
            manifest[name] = {
                "source": "wine-quality.zip",
                "sha256": digest(value),
                "bytes": len(value),
            }
    path = folder / "agentloop-v070-source.json"
    if path.exists():
        value = path.read_bytes()
    else:
        source = {
            name: download(
                f"https://raw.githubusercontent.com/dipeshbabu/agentloop/{REPOSITORY_REVISION}/{name}"
            ).decode("utf-8")
            for name in REPOSITORY_FILES
        }
        value = json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    if digest(value) != REPOSITORY_HASH:
        raise ValueError("released repository snapshot checksum mismatch")
    path.write_bytes(value)
    manifest[path.name] = {
        "source_revision": REPOSITORY_REVISION,
        "sha256": REPOSITORY_HASH,
        "bytes": len(value),
    }
    (folder / "source-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _symbol_span(body, symbol):
    parts = symbol.split(".")
    nodes = ast.parse(body).body
    for part in parts:
        node = next(
            item
            for item in nodes
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == part
        )
        nodes = node.body
    return node.lineno, node.end_lineno


def build_tasks(folder):
    folder = Path(folder)
    source = json.loads((folder / "agentloop-v070-source.json").read_text(encoding="utf-8"))
    definitions = [
        (
            "Which function in the profiler calculates one model call's cost from its pricing and usage evidence?",
            "agentloop/costs.py",
            "estimate_cost",
        ),
        (
            "Which function dispatches one output to the configured quality scorer?",
            "agentloop/quality.py",
            "score_output",
        ),
        (
            "Which method validates a serialized native trace before constructing its trace object?",
            "agentloop/tracer.py",
            "AgentTrace.from_dict",
        ),
        (
            "Which function rejects tool concurrency candidates with explicit dependencies or unsafe shared state?",
            "agentloop/parallelism.py",
            "parallelization_candidates",
        ),
        (
            "Which function summarizes the exactness of token counts across model events?",
            "agentloop/tokens.py",
            "token_status",
        ),
        (
            "Which function compares baseline and candidate traces and evaluates replay gates?",
            "agentloop/replay.py",
            "build_replay_report",
        ),
        (
            "Which function records original finding predictions together with a measured baseline/candidate comparison?",
            "agentloop/interventions.py",
            "build_intervention",
        ),
        (
            "Which function loads a study manifest and summarizes paired runs from its conditions?",
            "agentloop/studies.py",
            "summarize_study",
        ),
    ]
    tasks = []
    for index, (prompt, path, symbol) in enumerate(definitions):
        start, end = _symbol_span(source[path], symbol)
        tasks.append(
            {
                "id": f"repository-{index:02d}",
                "workload": "repository",
                "split": "pilot" if index < 2 else "evaluation",
                "prompt": prompt,
                "expected": {"path": path, "symbol": symbol, "start": start, "end": end},
                "label_ref": f"agentloop:{REPOSITORY_REVISION}:{path}:{start}",
            }
        )
    database = WineDatabase(
        (folder / "winequality-red.csv").read_text(), (folder / "winequality-white.csv").read_text()
    )
    sql_tasks = [
        (
            "How many red wine samples are in the dataset? Return one row with the count.",
            "SELECT count(*) FROM wines WHERE color='red'",
        ),
        (
            "What is the average quality of the white wines, rounded to 3 decimal places? Return one row.",
            "SELECT round(avg(quality),3) FROM wines WHERE color='white'",
        ),
        (
            "Count samples for each color. Return color and count ordered by color ascending.",
            "SELECT color,count(*) FROM wines GROUP BY color ORDER BY color",
        ),
        (
            "For red wines with quality at least 7, give the average alcohol rounded to 3 decimal places. Return one row.",
            "SELECT round(avg(alcohol),3) FROM wines WHERE color='red' AND quality>=7",
        ),
        (
            "What is the maximum residual sugar among white wines? Return one row.",
            "SELECT max(residual_sugar) FROM wines WHERE color='white'",
        ),
        (
            "Count samples with quality at most 4 for each color. Return color and count ordered by color ascending.",
            "SELECT color,count(*) FROM wines WHERE quality<=4 GROUP BY color ORDER BY color",
        ),
        (
            "Give the minimum and maximum pH for red wine in one row, in that order.",
            "SELECT min(ph),max(ph) FROM wines WHERE color='red'",
        ),
        (
            "For each quality score of at least 7, give quality and the average volatile acidity across both colors rounded to 3 decimals, ordered by quality ascending.",
            "SELECT quality,round(avg(volatile_acidity),3) FROM wines WHERE quality>=7 GROUP BY quality ORDER BY quality",
        ),
    ]
    try:
        for index, (prompt, sql) in enumerate(sql_tasks):
            tasks.append(
                {
                    "id": f"sql-{index:02d}",
                    "workload": "sql",
                    "split": "pilot" if index < 2 else "evaluation",
                    "prompt": prompt,
                    "expected": database.query(sql)["rows"],
                    "gold_sql": sql,
                    "label_ref": "independent frozen SQL over UCI Wine Quality; scorer tolerance 1e-6",
                }
            )
    finally:
        database.close()
    problems = [
        json.loads(line)
        for line in (folder / "gsm8k-test.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    selected = sorted(
        range(len(problems)), key=lambda index: digest(f"agentloop-study-20260923:{index}".encode())
    )[:8]
    for ordinal, index in enumerate(selected):
        item = problems[index]
        expected = item["answer"].rsplit("####", 1)[1].strip().replace(",", "")
        tasks.append(
            {
                "id": f"math-{ordinal:02d}",
                "workload": "math",
                "split": "pilot" if ordinal < 2 else "evaluation",
                "prompt": item["question"],
                "expected": expected,
                "source_index": index,
                "label_ref": f"GSM8K:{GSM_REVISION}:test:{index}",
            }
        )
    return tasks


def prepare(folder):
    folder = Path(folder)
    manifest = prepare_sources(folder / "sources")
    tasks = build_tasks(folder / "sources")
    protocol = {
        "schema_version": "1.0",
        "name": "Exploratory local real-agent intervention study",
        "phase": "pilot",
        "paid_provider_budget_usd": 0,
        "agentloop_version": "0.7.0",
        "langgraph_version": "1.2.11",
        "source_manifest": manifest,
        "tasks": tasks,
        "scorer_version": SCORER_VERSION,
        "sampling": "GSM8K hash ordering fixed before inference; repository/SQL tasks specified from independent source criteria",
        "planned_evaluation_task_count_rule": "Use all six evaluation tasks per workload unless the slowest completed pilot exceeds 60 seconds; then use the first four, retaining every task and reason in the plan.",
        "repetitions": 2,
        "pairing_keys": ["workload", "task_id", "repetition", "protocol_hash"],
        "max_model_calls": 4,
        "max_output_tokens": 192,
        "task_timeout_s": 180,
        "request_timeout_s": 90,
        "quality_acceptance": "Every planned candidate task must meet its independent criterion; no decrease from baseline; missing/failed tasks remain in denominators.",
        "latency_acceptance": "Descriptive paired effects plus native replay's non-regression gates; exploratory task-weighted uncertainty, not universal gains.",
        "cost_scope": "Paid-provider spend is zero. Self-hosted operating cost is unavailable; native cost gates remain indeterminate, never replaced by a fabricated zero rate.",
        "selection": "Select a single intervention family using pilot findings only; freeze configuration and original predictions before candidate evaluation. Retain rejected and unavailable findings.",
        "limits": "Public benchmark tasks and one host, not production traffic. GSM8K may overlap model training. Shared hardware and few independent tasks limit causal and general claims.",
    }
    path = folder / "pilot-protocol.json"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out))
