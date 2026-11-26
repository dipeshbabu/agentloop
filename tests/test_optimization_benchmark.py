"""The optimization benchmark's scorers, tools and published evidence (issue #406)."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from agentloop.pilot_records import METHOD_SCHEMA, compare_methods, validate_pilot_record
from examples.optimization_benchmark import agents, run, scoring

EVIDENCE = Path(__file__).resolve().parents[1] / "research" / "optimization-benchmark-2026-11"


# Scorers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "gold", "expected"),
    [("The answer is 72", "72", 1.0), ("72.0", "72", 1.0), ("1,234", "1234", 1.0),
     ("about 71", "72", 0.0), (None, "72", 0.0), ("no number", "72", 0.0)],
)  # fmt: skip
def test_gsm8k_takes_the_last_number(output, gold, expected):
    assert scoring.score_gsm8k(output, gold) == expected
    assert scoring.gsm8k_gold("work...\n#### 1,234") == "1234"


def test_squad_uses_official_normalization_and_unanswerable():
    assert scoring.score_squad("The Normans.", ["Normans"]) == 1.0
    assert 0 < scoring.score_squad("in the 10th century", ["10th and 11th centuries"]) < 1
    assert scoring.score_squad("unanswerable", []) == 1.0
    assert scoring.score_squad("", []) == 1.0
    assert scoring.score_squad("France", []) == 0.0


def test_humaneval_runs_hidden_tests_in_a_child():
    task = {"test": "def check(f):\n    assert f(2) == 4\n", "entry_point": "double"}
    assert scoring.score_humaneval("def double(x):\n    return 2 * x\n", task) == 1.0
    assert scoring.score_humaneval("def double(x):\n    return x\n", task) == 0.0
    assert scoring.score_humaneval(None, task) == 0.0
    slow = "import time\ndef double(x):\n    time.sleep(10)\n"
    assert scoring.score_humaneval(slow, task, timeout_s=1) == 0.0


# Agent tools --------------------------------------------------------------------------


def test_calculator_is_restricted():
    assert agents.calculate("3 * (4 + 5) / 2") == 13.5
    assert agents.calculate("2^3") == 8
    for bad in ("__import__('os')", "10 ** 100", "a + 1"):
        with pytest.raises((ValueError, SyntaxError)):
            agents.calculate(bad)


def test_prompt_examples_and_self_test():
    prompt = 'def inc(x):\n    """\n    >>> inc(1)\n    2\n    """\n'
    assert agents.prompt_examples(prompt) == [("inc(1)", "2")]
    assert agents.run_examples("def inc(x):\n    return x + 1\n", [("inc(1)", "2")]) is None
    assert "returned" in agents.run_examples("def inc(x):\n    return x\n", [("inc(1)", "2")])


def test_prefix_keeps_prompt_imports_and_helpers():
    prompt = "from typing import List\n\n\ndef helper():\n    pass\n\n\ndef target(x):\n"
    code = "def target(x):\n    return helper()\n"
    joined = agents._with_prefix(prompt, "target", code)
    assert joined.startswith("from typing import List") and joined.endswith(code)
    assert agents._with_prefix(prompt, "target", joined) == joined


def test_find_sentences_ranks_by_overlap():
    context = "Paris is in France. Berlin is in Germany. Rome is in Italy."
    assert agents.find_sentences(context, "Where is Berlin?")[0] == "Berlin is in Germany."


def test_finding_mapping_is_per_family():
    assert run._changes("route_to_smaller_model", "coding") == {"model": "small"}
    assert run._changes("parallelize_tools", "extraction") == {"parallel_retrieval": True}
    assert run._changes("batch_model_calls", "math") == {}


# Published evidence ---------------------------------------------------------------------


def _read(name):
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def test_published_comparison_recomputes_from_held_out_rows():
    rows = _read("held-out.json")
    kinds = {"none": "no_optimization", "heuristic": "heuristic_ranking", "manual": "manual",
             "agentloop": "agentloop_pass"}  # fmt: skip
    method_rows = [
        {"schema": METHOD_SCHEMA, "workload": f"{r['family']}:{r['task']}".replace("/", "_")[:64],
         "method": r["method"], "method_kind": kinds[r["method"]], "split": "held_out",
         "negative_control": r["family"] == run.CONTROL, "quality_source": "independent",
         "quality": r["quality"], "runtime_ms": r["runtime_ms"], "cost_usd": None}
        for r in rows
    ]  # fmt: skip
    assert compare_methods(method_rows) == _read("results.json")["comparison"]


def test_published_summary_matches_rows_and_protocol():
    rows, results, protocol = _read("held-out.json"), _read("results.json"), _read("protocol.json")
    for family, methods in results["summary"].items():
        assert len(protocol["splits"][family]["held_out"]) == 25
        for method, summary in methods.items():
            mine = [r for r in rows if r["family"] == family and r["method"] == method]
            assert summary["tasks"] == len(mine) == 25
            assert summary["quality_mean"] == round(statistics.fmean(r["quality"] for r in mine), 4)
    # The derivation never touched held-out tasks.
    dev = {r["task"] for r in _read("dev-baseline.json")}
    held = {t for family in protocol["splits"].values() for t in family["held_out"]}
    assert not dev & held


def test_identical_configurations_give_identical_quality():
    rows = _read("held-out.json")
    by = {(r["family"], r["task"], r["method"]): r for r in rows}
    methods = _read("derivation.json")["methods"]
    for (family, task, method), row in by.items():
        if method == "agentloop" and methods[family]["agentloop"] == methods[family]["none"]:
            assert row["quality"] == by[(family, task, "none")]["quality"]


def test_pilot_record_is_valid_and_labelled_as_an_ai_participant():
    record = _read("pilot/pilot-record.json")
    checked = validate_pilot_record(record)
    assert record["participant_kind"] == "ai_coding_agent"
    assert "not independent developer adoption" in record["limits"]
    assert checked["minutes_to_outcome"] == 8.83


def test_evidence_contains_no_dataset_text():
    for path in EVIDENCE.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert '"input_text": "' not in text.replace('"input_text": null', ""), path
