from __future__ import annotations

import fnmatch
import importlib
import json
import math
from pathlib import Path
from typing import Any

from agentloop.markdown import markdown_table_cell

_MAX_GLOB_PATTERN_LENGTH = 256
_MAX_GLOB_TEXT_LENGTH = 1_000_000
_MISSING = object()
_SCORER_TYPES = {
    "contains",
    "custom",
    "exact_match",
    "glob",
    "json_schema",
    "json_subset",
    "regex",
    "required_fields",
}


class QualityValidationError(ValueError):
    """Raised when a quality suite cannot safely act as a correctness gate."""


def parse_quality_fixtures(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        fixtures = payload
    elif isinstance(payload, dict):
        if "fixtures" not in payload:
            raise QualityValidationError("top-level object must contain a 'fixtures' list")
        fixtures = payload["fixtures"]
    else:
        raise QualityValidationError("quality fixtures must be a JSON list or object")
    return validate_quality_fixtures(fixtures)


def load_quality_fixtures(path: str | Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QualityValidationError(
            f"fixture file is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    return parse_quality_fixtures(payload)


def validate_quality_fixtures(fixtures: Any) -> list[dict[str, Any]]:
    if not isinstance(fixtures, list):
        raise QualityValidationError("'fixtures' must be a list")
    if not fixtures:
        raise QualityValidationError("quality fixture suite must contain at least one case")

    for index, fixture in enumerate(fixtures):
        path = f"fixtures[{index}]"
        if not isinstance(fixture, dict):
            raise QualityValidationError(f"{path} must be an object")
        if "id" in fixture and (not isinstance(fixture["id"], str) or not fixture["id"].strip()):
            raise QualityValidationError(f"{path}.id must be a non-empty string")

        scorer = fixture.get("scorer", {"type": "exact_match"})
        if not isinstance(scorer, dict):
            raise QualityValidationError(f"{path}.scorer must be an object")
        scorer_type = scorer.get("type", "exact_match")
        if not isinstance(scorer_type, str) or not scorer_type.strip():
            raise QualityValidationError(f"{path}.scorer.type must be a non-empty string")
        if scorer_type != scorer_type.strip():
            raise QualityValidationError(
                f"{path}.scorer.type must not contain leading or trailing whitespace"
            )
        if scorer_type not in _SCORER_TYPES:
            raise QualityValidationError(
                f"{path}.scorer.type is unsupported: {scorer_type}; "
                f"expected one of {', '.join(sorted(_SCORER_TYPES))}"
            )

        if scorer_type == "exact_match":
            if not _has_configured_value(fixture, scorer, "expected", "expected"):
                raise QualityValidationError(
                    f"{path} exact_match scorer requires an explicit 'expected' value"
                )
        elif scorer_type == "contains":
            value = _configured_value(fixture, scorer, "expected", "text")
            if not isinstance(value, str) or not value.strip():
                raise QualityValidationError(
                    f"{path} contains scorer requires non-empty 'expected' or 'scorer.text'"
                )
        elif scorer_type in {"glob", "regex"}:
            value = _configured_value(fixture, scorer, "expected", "pattern")
            if not isinstance(value, str) or not value:
                raise QualityValidationError(
                    f"{path} {scorer_type} scorer requires a non-empty pattern"
                )
        elif scorer_type in {"required_fields", "json_schema"}:
            required = scorer.get("required", scorer.get("required_fields"))
            if not isinstance(required, list) or not required:
                raise QualityValidationError(
                    f"{path} {scorer_type} scorer requires a non-empty field list"
                )
            if any(not isinstance(field, str) or not field.strip() for field in required):
                raise QualityValidationError(
                    f"{path} {scorer_type} scorer fields must be non-empty strings"
                )
        elif scorer_type == "json_subset":
            expected = scorer.get("expected", fixture.get("expected"))
            expected_data = _json_value(expected)
            if not isinstance(expected_data, dict) or not expected_data:
                raise QualityValidationError(
                    f"{path} json_subset scorer requires a non-empty expected object"
                )
        elif scorer_type == "custom":
            target = scorer.get("callable")
            if (
                not isinstance(target, str)
                or target.count(":") != 1
                or any(not part.strip() for part in target.split(":"))
            ):
                raise QualityValidationError(
                    f"{path} custom scorer callable must use module:function"
                )

    return fixtures


def build_quality_report(
    fixtures: list[dict[str, Any]],
    *,
    baseline_trace: Any | None = None,
    candidate_trace: Any | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    validate_quality_fixtures(fixtures)
    if min_score is not None:
        _bounded_score(min_score, "min_score")

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
    if scorer_type == "regex":
        return _result(
            False,
            "raw regular-expression scorers are disabled; use glob, contains, or exact_match",
        )
    if output is _MISSING:
        return _result(False, "output is missing")
    if scorer_type == "exact_match":
        expected = _configured_value(fixture, scorer, "expected", "expected")
        passed = _exactly_equal(output, expected)
        return _result(passed, "exact match" if passed else "output did not exactly match expected")
    if scorer_type == "contains":
        expected = str(_configured_value(fixture, scorer, "expected", "text"))
        passed = expected in _text_output(output)
        return _result(
            passed, "required text present" if passed else f"missing required text: {expected}"
        )
    if scorer_type == "glob":
        pattern = str(_configured_value(fixture, scorer, "expected", "pattern"))
        text = _text_output(output)
        if len(pattern) > _MAX_GLOB_PATTERN_LENGTH:
            return _result(False, "glob pattern exceeds the 256-character safety limit")
        if len(text) > _MAX_GLOB_TEXT_LENGTH:
            return _result(False, "output exceeds the 1,000,000-character glob safety limit")
        passed = fnmatch.fnmatchcase(text, pattern)
        return _result(passed, "glob matched" if passed else "glob did not match")
    if scorer_type in {"required_fields", "json_schema"}:
        return _score_required_fields(output, scorer)
    if scorer_type == "json_subset":
        expected = scorer["expected"] if "expected" in scorer else fixture.get("expected", {})
        return _score_json_subset(output, expected)
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
    missing = [key for key in expected_data if key not in data]
    mismatches = [
        key
        for key, value in expected_data.items()
        if key in data and not _exactly_equal(data[key], value)
    ]
    problems = []
    if missing:
        problems.append(f"missing keys: {', '.join(str(key) for key in missing)}")
    if mismatches:
        problems.append(f"mismatched keys: {', '.join(str(key) for key in mismatches)}")
    return _result(
        not problems,
        "JSON subset matched" if not problems else "; ".join(problems),
    )


def _score_custom(output: Any, fixture: dict[str, Any], scorer: dict[str, Any]) -> dict[str, Any]:
    target = str(scorer.get("callable") or "")
    if ":" not in target:
        return _result(False, "custom scorer callable must use module:function")
    module_name, function_name = target.split(":", 1)
    try:
        fn = getattr(importlib.import_module(module_name), function_name)
        raw = fn(output, fixture, scorer)
    except Exception as exc:
        raise QualityValidationError(f"custom scorer {target} failed: {exc}") from exc
    if isinstance(raw, dict):
        if not isinstance(raw.get("passed"), bool):
            raise QualityValidationError(
                f"custom scorer {target} must return a boolean 'passed' field"
            )
        score = _bounded_score(
            raw.get("score", 1.0 if raw["passed"] else 0.0),
            f"custom scorer {target} score",
        )
        return {
            "score": score,
            "passed": raw["passed"],
            "detail": str(raw.get("detail", "")),
        }
    if isinstance(raw, bool):
        return _result(raw, "custom scorer passed" if raw else "custom scorer failed")
    raise QualityValidationError(f"custom scorer {target} must return a boolean or result object")


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
            f"{markdown_table_cell(case['case_id'])} | "
            f"{status} | "
            f"{case['baseline']['score']:.4f} | "
            f"{case['candidate']['score']:.4f} | "
            f"{markdown_table_cell(case['candidate']['detail'])} |"
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
        return _MISSING
    metadata = getattr(trace, "metadata", {}) or {}
    if "output" in metadata:
        return metadata["output"]
    for event in reversed(getattr(trace, "events", [])):
        if (
            getattr(event, "event_type", "") == "model_call"
            and getattr(event, "output_text", None) is not None
        ):
            return event.output_text
    return _MISSING


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _exactly_equal(actual: Any, expected: Any) -> bool:
    """Compare values without Python's cross-type equality coercions."""

    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            _exactly_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list | tuple):
        return len(actual) == len(expected) and all(
            _exactly_equal(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _text_output(value: Any) -> str:
    return "" if value is None else str(value)


def _average(values: Any) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(float(item) for item in items) / len(items)


def _result(passed: bool, detail: str) -> dict[str, Any]:
    return {"score": 1.0 if passed else 0.0, "passed": passed, "detail": detail}


def _is_empty(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or (isinstance(value, list | dict) and not value)


def _configured_value(
    fixture: dict[str, Any], scorer: dict[str, Any], fixture_key: str, scorer_key: str
) -> Any:
    return fixture[fixture_key] if fixture_key in fixture else scorer.get(scorer_key)


def _has_configured_value(
    fixture: dict[str, Any], scorer: dict[str, Any], fixture_key: str, scorer_key: str
) -> bool:
    return fixture_key in fixture or scorer_key in scorer


def _require_non_empty(value: Any, path: str) -> None:
    if _is_empty(value):
        raise QualityValidationError(f"{path} must be configured and non-empty")


def _bounded_score(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QualityValidationError(f"{name} must be a number between 0 and 1")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise QualityValidationError(f"{name} must be between 0 and 1")
    return score
