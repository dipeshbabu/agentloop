"""Keep the finding benchmark guide's numbers tied to the checked-in evidence.

docs/FINDING_BENCHMARKS.md reports two snapshots of the same frozen corpus: the
archived baseline (examples/finding_benchmark_baseline.json) and the current
rules evaluated on the same evaluation split. Both tables sit between marker
comments and are regenerated here; a mismatch prints the expected table so the
guide can be updated deliberately rather than drifting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import run_finding_benchmark, summarize_finding_benchmark

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/FINDING_BENCHMARKS.md"
BASELINE = ROOT / "examples/finding_benchmark_baseline.json"
CORPUS = ROOT / "examples/finding_benchmark_corpus.json"
CONTENT_BASELINE = ROOT / "examples/finding_benchmark_content_baseline.json"
CONTENT_CORPUS = ROOT / "examples/finding_benchmark_content_corpus.json"


@pytest.fixture(scope="module")
def corpus():
    return FindingBenchmark(json.loads(CORPUS.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshots(corpus, baseline):
    archived = summarize_finding_benchmark(corpus, baseline)
    current = summarize_finding_benchmark(
        corpus,
        run_finding_benchmark(corpus, split="evaluation", source_revision="docs:current"),
    )
    return archived, current


def ratio(numerator: int, denominator: int) -> str:
    """A count with its denominator; a rate only when the denominator is nonzero."""
    if denominator == 0:
        return f"{numerator} / 0 (unavailable)"
    return f"{numerator} / {denominator} ({numerator / denominator:.2f})"


def versions(summary) -> str:
    found = sorted({item["rule_version"] for item in summary["per_rule"]})
    return ", ".join(found)


def overall_row(label: str, summary) -> str:
    o = summary["overall"]
    scored = o["true_positive_findings"] + o["false_positive_findings"]
    return (
        f"| {label} | {versions(summary)} | {o['planned_cases']} "
        f"| {ratio(o['true_positive_findings'], scored)} "
        f"| {ratio(o['negative_cases_with_findings'], o['negative_cases'])} "
        f"| {ratio(o['emitted_cases'], o['planned_cases'])} "
        f"| {o['unscored_findings']} | {o['missed_opportunities']} |"
    )


def expected_overall(archived, current, revision: str) -> str:
    return "\n".join(
        [
            "| Snapshot | Rule versions | Planned cases | Precision (correct / scored findings) "
            "| Known negatives with any finding | Cases with a finding | Unscored findings "
            "| Missed opportunities |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            overall_row(f"Archived baseline (`{revision[:7]}`)", archived),
            overall_row("Current rules", current),
        ]
    )


def expected_per_rule(archived, current) -> str:
    now = {item["rule_id"]: item for item in current["per_rule"]}
    lines = [
        "| Rule | Cases | Archived version: correct / false | Current version: correct / false |",
        "| --- | --- | --- | --- |",
    ]
    for item in archived["per_rule"]:
        rule = item["rule_id"]
        later = now.get(rule)
        current_cell = (
            "not evaluated"
            if later is None
            else f"{later['rule_version']}: {later['true_positive_findings']} / "
            f"{later['false_positive_findings']}"
        )
        lines.append(
            f"| `{rule}` | {item['planned_cases']} | {item['rule_version']}: "
            f"{item['true_positive_findings']} / {item['false_positive_findings']} "
            f"| {current_cell} |"
        )
    return "\n".join(lines)


def marked(name: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(rf"<!-- {name}:start -->\n(.*?)\n<!-- {name}:end -->", text, flags=re.DOTALL)
    assert match, f"docs/FINDING_BENCHMARKS.md is missing the {name} markers"
    return match.group(1).strip()


def test_overall_table_matches_baseline_and_current_rules(snapshots, baseline):
    expected = expected_overall(*snapshots, baseline["source_revision"])
    assert marked("finding-benchmark-overall") == expected, (
        "Update the overall table in docs/FINDING_BENCHMARKS.md to:\n" + expected
    )


def test_per_rule_table_matches_baseline_and_current_rules(snapshots):
    expected = expected_per_rule(*snapshots)
    assert marked("finding-benchmark-per-rule") == expected, (
        "Update the per-rule table in docs/FINDING_BENCHMARKS.md to:\n" + expected
    )


def test_guide_cites_the_archived_snapshot_identity(baseline):
    text = GUIDE.read_text(encoding="utf-8")
    assert baseline["source_revision"] in text
    assert baseline["corpus_hash"] in text
    assert baseline["split"] == "evaluation"


def test_corpus_description_matches_checked_in_cases(corpus):
    cases = corpus.to_dict()["cases"]
    splits = [case["split"] for case in cases]
    text = GUIDE.read_text(encoding="utf-8")
    total, development = len(cases), splits.count("development")
    assert f"has {total} synthetic cases" in text
    assert f"{development} development cases and {total - development} evaluation cases" in text


@pytest.mark.parametrize(
    ("numerator", "denominator", "text"),
    [(8, 10, "8 / 10 (0.80)"), (0, 9, "0 / 9 (0.00)"), (0, 0, "0 / 0 (unavailable)")],
)
def test_ratio_keeps_denominators_and_never_divides_by_zero(numerator, denominator, text):
    assert ratio(numerator, denominator) == text


def test_content_corpus_table_matches_its_baseline_and_current_rules():
    corpus = FindingBenchmark(json.loads(CONTENT_CORPUS.read_text(encoding="utf-8")))
    baseline = json.loads(CONTENT_BASELINE.read_text(encoding="utf-8"))
    archived = summarize_finding_benchmark(corpus, baseline)
    current = summarize_finding_benchmark(
        corpus,
        run_finding_benchmark(corpus, split="evaluation", source_revision="docs:current"),
    )
    revision = baseline["source_revision"]
    expected = expected_overall(archived, current, revision).replace(
        f"Archived baseline (`{revision[:7]}`)", f"Recorded baseline (`{revision[:7]}`)"
    )
    assert marked("content-finding-benchmark-overall") == expected, (
        "Update the content corpus table in docs/FINDING_BENCHMARKS.md to:\n" + expected
    )
    text = GUIDE.read_text(encoding="utf-8")
    assert revision in text and baseline["corpus_hash"] in text
    cases = corpus.to_dict()["cases"]
    development = [case["split"] for case in cases].count("development")
    assert f"It has {len(cases)} synthetic cases" in text
    assert (
        f"{development} development cases and {len(cases) - development} evaluation cases" in text
    )
