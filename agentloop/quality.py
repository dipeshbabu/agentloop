from __future__ import annotations

import fnmatch
import importlib
import json
from pathlib import Path
from typing import Any

_MAX_GLOB_PATTERN_LENGTH = 256
_MAX_GLOB_TEXT_LENGTH = 1_000_000


def load_quality_fixtures(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    return list(payload.get("fixtures", []))


def build_quality_report(
    fixtures: list[dict[str, Any]],
    *,
    baseline_trace: Any | None = None,
    candidate_trace: Any | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    cases = []
    for index, fixture in enumerate(fixtures, start=1):
        case_id = str(fixture.get("id") or f"case_{index:03d}")
        baseline_output = _output_for_side(fixture, "baseline", baseline_trace)
        candidate_output = _output_for_side(fixture, "candidate", candidate_trace)
        scorer = fixture.get("scorer", {"type": "exact_match"})
        baseline_result = score_output(baseline_output, fixture, scorer)
        candidate_result = score_output(candidate_output, fixture, scorer)
        cases.append(
            {
                "case_id": case_id,
                "input": fixture.get("input"),
                "scorer": scorer,
                "baseline": baseline_result,
                "candidate": candidate_result,
                "passed": candidate_result["passed"],
            }
        )

    baseline_score = _average(case["baseline"]["score"] for case in cases)
    candidate_score = _average(case["candidate"]["score"] for case in cases)
    failed_cases = [case for case in cases if not case["passed"]]
    passed = not failed_cases
    if min_score is not None:
        passed = passed and candidate_score >= min_score

    return {
        "case_count": len(cases),
        "passed": passed,
        "baseline_score": round(baseline_score, 4),
        "candidate_score": round(candidate_score, 4),
        "quality_delta": round(candidate_score - baseline_score, 4),
        "min_score": min_score,
        "failed_case_count": len(failed_cases),
        "failed_cases": failed_cases,
        "cases": cases,
    }


def score_output(output: Any, fixture: dict[str, Any], scorer: dict[str, Any]) -> dict[str, Any]:
    scorer_type = str(scorer.get("type", "exact_match"))
    if scorer_type == "exact_match":
        expected = fixture.get("expected", scorer.get("expected"))
        passed = _normalize(output) == _normalize(expected)
        return _result(passed, "exact match" if passed else "output did not exactly match expected")
    if scorer_type == "contains":
        expected = str(fixture.get("expected", scorer.get("text", "")))
        passed = expected in str(output or "")
        return _result(
            passed, "required text present" if passed else f"missing required text: {expected}"
        )
    if scorer_type == "glob":
        pattern = str(scorer.get("pattern") or fixture.get("expected") or "")
        text = str(output or "")
        if len(pattern) > _MAX_GLOB_PATTERN_LENGTH:
            return _result(False, "glob pattern exceeds the 256-character safety limit")
        if len(text) > _MAX_GLOB_TEXT_LENGTH:
            return _result(False, "output exceeds the 1,000,000-character glob safety limit")
        passed = fnmatch.fnmatchcase(text, pattern)
        return _result(passed, "glob matched" if passed else "glob did not match")
    if scorer_type == "regex":
        return _result(
            False,
            "raw regular-expression scorers are disabled; use glob, contains, or exact_match",
        )
    if scorer_type in {"required_fields", "json_schema"}:
        return _score_required_fields(output, scorer)
    if scorer_type == "json_subset":
        return _score_json_subset(output, scorer.get("expected", fixture.get("expected", {})))
    if scorer_type == "custom":
        return _score_custom(output, fixture, scorer)
    return _result(False, f"unknown scorer type: {scorer_type}")


def _score_required_fields(output: Any, scorer: dict[str, Any]) -> dict[str, Any]:
    required = list(scorer.get("required") or scorer.get("required_fields") or [])
    data = _json_value(output)
    if not isinstance(data, dict):
        return _result(False, "output is not a JSON object")
    missing = [field for field in required if field not in data or _is_empty(data[field])]
    return _result(
        not missing,
        "required fields present" if not missing else f"missing fields: {', '.join(missing)}",
    )


def _score_json_subset(output: Any, expected: Any) -> dict[str, Any]:
    data = _json_value(output)
    expected_data = _json_value(expected)
    if not isinstance(data, dict) or not isinstance(expected_data, dict):
        return _result(False, "output and expected must be JSON objects")
    mismatches = [key for key, value in expected_data.items() if data.get(key) != value]
    return _result(
        not mismatches,
        "JSON subset matched" if not mismatches else f"mismatched keys: {', '.join(mismatches)}",
    )


def _score_custom(output: Any, fixture: dict[str, Any], scorer: dict[str, Any]) -> dict[str, Any]:
    target = str(scorer.get("callable") or "")
    if ":" not in target:
        return _result(False, "custom scorer callable must use module:function")
    module_name, function_name = target.split(":", 1)
    fn = getattr(importlib.import_module(module_name), function_name)
    raw = fn(output, fixture, scorer)
    if isinstance(raw, dict):
        return {
            "score": float(raw.get("score", 1.0 if raw.get("passed") else 0.0)),
            "passed": bool(raw.get("passed")),
            "detail": str(raw.get("detail", "")),
        }
    passed = bool(raw)
    return _result(passed, "custom scorer passed" if passed else "custom scorer failed")


def quality_report_to_markdown(report: dict[str, Any]) -> str:
    status = "passed" if report["passed"] else "failed"
    lines = [
        "# AgentLoop Quality Report",
        "",
        f"- Status: {status}",
        f"- Cases: {report['case_count']}",
        f"- Baseline score: {report['baseline_score']:.4f}",
        f"- Candidate score: {report['candidate_score']:.4f}",
        f"- Quality delta: {report['quality_delta']:.4f}",
        f"- Failed cases: {report['failed_case_count']}",
        "",
        "## Cases",
        "",
        "| Case | Status | Baseline | Candidate | Detail |",
        "|---|---|---:|---:|---|",
    ]
    for case in report["cases"]:
        status = "pass" if case["passed"] else "fail"
        lines.append(
            "| "
            f"{case['case_id']} | "
            f"{status} | "
            f"{case['baseline']['score']:.4f} | "
            f"{case['candidate']['score']:.4f} | "
            f"{_cell(case['candidate']['detail'])} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _output_for_side(fixture: dict[str, Any], side: str, trace: Any | None) -> Any:
    key = f"{side}_output"
    if key in fixture:
        return fixture[key]
    if side == "candidate" and "output" in fixture:
        return fixture["output"]
    return _trace_output(trace)


def _trace_output(trace: Any | None) -> Any:
    if trace is None:
        return ""
    metadata = getattr(trace, "metadata", {}) or {}
    if "output" in metadata:
        return metadata["output"]
    for event in reversed(getattr(trace, "events", [])):
        if getattr(event, "event_type", "") == "model_call" and getattr(event, "output_text", None):
            return event.output_text
    return ""


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _normalize(value: Any) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return str(value or "").strip()


def _average(values: Any) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(float(item) for item in items) / len(items)


def _result(passed: bool, detail: str) -> dict[str, Any]:
    return {"score": 1.0 if passed else 0.0, "passed": passed, "detail": detail}


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
