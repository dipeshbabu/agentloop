"""Versioned, offline scoring for structured workflow outputs."""

from __future__ import annotations

import importlib
import inspect
import json
import math
from collections import Counter
from fractions import Fraction
from hashlib import sha256
from types import SimpleNamespace

CONTRACT_VERSION = "2.0"
EVIDENCE_KEY = "agentloop.quality"
SCORER_TYPES = ("decision", "multilabel", "numeric", "fields", "matches")
LEGACY_TYPES = ("contains", "custom", "exact_match", "glob", "json_subset", "required_fields")
_COMMON = {"type", "version", "pass_score"}
_ALLOWED = {
    "decision": {"labels", "field"},
    "multilabel": {"labels", "field"},
    "numeric": {"minimum", "maximum", "tolerance", "field", "unit"},
    "fields": {"allow_extra", "field"},
    "matches": {"symmetric", "field"},
    "exact_match": {"expected"},
    "contains": {"text"},
    "glob": {"pattern"},
    "json_subset": {"expected"},
    "required_fields": {"required", "required_fields"},
    "custom": {"callable", "configuration", "capture_detail"},
}
_EXECUTION = {
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "timed_out",
    "stopped",
    "incomplete",
    "running",
    "unknown",
    "unreported",
}


def _fail(message):
    from agentloop.quality import QualityValidationError

    raise QualityValidationError(message)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _fail("structured quality values must be finite JSON")


def _hash(value):
    return sha256(_canonical(value).encode()).hexdigest()


def _finite(value):
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def _label(value):
    if value is not None and type(value) not in {str, int, float, bool}:
        _fail("decision labels and entity IDs must be JSON scalars")
    return _canonical([type(value).__name__, value])


def _is_hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def uses_contract(fixtures):
    return isinstance(fixtures, list) and any(
        isinstance(item, dict) and item.get("schema_version") == CONTRACT_VERSION
        for item in fixtures
    )


def validate_fixtures(fixtures):
    from agentloop.quality import validate_quality_fixtures

    if not isinstance(fixtures, list) or not fixtures:
        _fail("structured quality requires a nonempty fixture list")
    ids = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get("schema_version") != CONTRACT_VERSION:
            _fail("every structured fixture must declare schema_version 2.0")
        allowed = {
            "schema_version",
            "id",
            "expected",
            "scorer",
            "baseline_output",
            "candidate_output",
            "output",
            "input_ref",
            "expected_ref",
            "baseline_output_ref",
            "candidate_output_ref",
            "baseline_status",
            "candidate_status",
        }
        if set(fixture) - allowed:
            _fail("unknown structured fixture field")
        identity = fixture.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in ids:
            _fail("structured fixture IDs must be nonempty and unique")
        ids.add(identity)
        scorer = fixture.get("scorer")
        if (
            not isinstance(scorer, dict)
            or not isinstance(scorer.get("type"), str)
            or scorer["type"] not in _ALLOWED
        ):
            _fail("unsupported structured scorer type")
        kind = scorer["type"]
        if set(scorer) - (_COMMON | _ALLOWED[kind]):
            _fail(f"unknown {kind} scorer configuration field")
        version = scorer.get("version", "1.0" if kind != "custom" else None)
        if (
            not isinstance(version, str)
            or not version.strip()
            or (kind != "custom" and version != "1.0")
        ):
            _fail("built-in scorer version must be 1.0; custom scorers require an explicit version")
        if "pass_score" in scorer and (
            not _finite(scorer["pass_score"]) or not 0 <= scorer["pass_score"] <= 1
        ):
            _fail("pass_score must be in [0,1]")
        if "capture_detail" in scorer and type(scorer["capture_detail"]) is not bool:
            _fail("capture_detail must be a boolean")
        if "field" in scorer and (not isinstance(scorer["field"], str) or not scorer["field"]):
            _fail("field must be a nonempty top-level object key")
        for key in ("input_ref", "expected_ref", "baseline_output_ref", "candidate_output_ref"):
            if key in fixture and (not isinstance(fixture[key], str) or not fixture[key]):
                _fail(f"{key} must be a nonempty reference")
        for side in ("baseline", "candidate"):
            if f"{side}_status" in fixture and (
                not isinstance(fixture[f"{side}_status"], str)
                or fixture[f"{side}_status"] not in _EXECUTION
            ):
                _fail("unsupported execution status")
        if kind in {"decision", "multilabel"}:
            labels = scorer.get("labels")
            if (
                not isinstance(labels, list)
                or not labels
                or len({_label(item) for item in labels}) != len(labels)
            ):
                _fail("labels must be a nonempty list of distinct typed JSON scalars")
        elif kind == "numeric":
            low, high, tolerance = (
                scorer.get("minimum"),
                scorer.get("maximum"),
                scorer.get("tolerance", 0),
            )
            if (
                not all(_finite(value) for value in (low, high, tolerance))
                or low >= high
                or tolerance < 0
            ):
                _fail("numeric scorers require finite minimum < maximum and nonnegative tolerance")
            if "unit" in scorer and (not isinstance(scorer["unit"], str) or not scorer["unit"]):
                _fail("unit must be a nonempty identifier")
        elif kind in {"fields", "matches"}:
            key = "allow_extra" if kind == "fields" else "symmetric"
            if key in scorer and type(scorer[key]) is not bool:
                _fail(f"{key} must be a boolean")
        if kind in LEGACY_TYPES:
            legacy = {key: value for key, value in fixture.items() if key != "schema_version"}
            legacy["scorer"] = {
                key: value for key, value in scorer.items() if key not in _COMMON - {"type"}
            }
            try:
                validate_quality_fixtures([legacy])
            except (TypeError, RecursionError, OverflowError):
                _fail(
                    "legacy scorer configuration cannot be interpreted in the structured contract"
                )
        _canonical(fixture)
    return fixtures


def _result(score, status, detail, *, counts=None, error_type=None):
    return {
        "score": score,
        "status": status,
        "detail": detail,
        "counts": counts or {},
        "error_type": error_type,
    }


def _set_scores(expected, actual, *, label_domain=None):
    if not isinstance(expected, list):
        return _result(None, "invalid_expected", "expected a set-like JSON list")
    if not isinstance(actual, list):
        return _result(0.0, "invalid_output", "output must be a JSON list")
    try:
        left = [_label(value) for value in expected]
    except ValueError:
        return _result(None, "invalid_expected", "reference set members must be scalar labels")
    try:
        right = [_label(value) for value in actual]
    except ValueError:
        return _result(0.0, "invalid_output", "set members must be scalar labels")
    if len(set(left)) != len(left):
        return _result(None, "invalid_expected", "duplicate expected labels")
    if len(set(right)) != len(right):
        return _result(0.0, "invalid_output", "duplicate output labels")
    if label_domain is not None and not set(left) <= label_domain:
        return _result(None, "unknown_expected", "expected label is outside the declared domain")
    unknown = len(set(right) - label_domain) if label_domain is not None else 0
    tp, fp, fn = (
        len(set(left) & set(right)),
        len(set(right) - set(left)),
        len(set(left) - set(right)),
    )
    denominator = 2 * tp + fp + fn
    score = 2 * tp / denominator if denominator else 1.0
    return _result(
        score,
        "unknown_label" if unknown else "evaluated",
        "set F1",
        counts={
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "unknown_labels": unknown,
        },
    )


def _pairs(value, symmetric):
    if not isinstance(value, list):
        return None
    pairs = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return None
        pair = [_label(part) for part in item]
        pairs.append(_canonical(sorted(pair) if symmetric else pair))
    return pairs


def _custom(output, fixture, scorer):
    module, name = scorer["callable"].split(":", 1)
    try:
        function = getattr(importlib.import_module(module), name)
        if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
            return _result(None, "scorer_error", "custom scorer must be synchronous")
        raw = function(
            json.loads(_canonical(output)),
            json.loads(_canonical(fixture)),
            json.loads(_canonical(scorer)),
        )
        if inspect.iscoroutine(raw):
            raw.close()
            return _result(None, "scorer_error", "custom scorer returned a coroutine")
        if isinstance(raw, bool):
            return {
                **_result(float(raw), "evaluated", "custom scorer result"),
                "custom_passed": raw,
            }
        if not isinstance(raw, dict) or type(raw.get("passed")) is not bool:
            return _result(None, "scorer_error", "custom scorer returned an invalid result")
        score = raw.get("score", float(raw["passed"]))
        if not _finite(score) or not 0 <= score <= 1:
            return _result(None, "scorer_error", "custom scorer returned an invalid score")
        result = _result(
            float(score),
            "evaluated",
            str(raw.get("detail", ""))[:1024]
            if scorer.get("capture_detail")
            else "custom scorer result",
        )
        result["custom_passed"] = raw["passed"]
        return result
    except TimeoutError as exc:
        return _result(
            None, "scorer_timeout", "custom scorer timed out", error_type=type(exc).__name__
        )
    except Exception as exc:
        return _result(None, "scorer_error", "custom scorer failed", error_type=type(exc).__name__)


def score_output(output, fixture, scorer):
    from agentloop.quality import (
        _MISSING,
        _exactly_equal,
        _json_value,
    )
    from agentloop.quality import (
        score_output as legacy_score,
    )

    kind = scorer["type"]
    expected = fixture.get("expected", scorer.get("expected", _MISSING))
    if output is _MISSING:
        return _result(None, "missing_output", "output is unavailable")
    if "field" in scorer:
        parsed = _json_value(output)
        if not isinstance(parsed, dict) or scorer["field"] not in parsed:
            return _result(0.0, "invalid_output", "selected output field is absent")
        output = parsed[scorer["field"]]
    if kind in {"decision", "multilabel", "numeric", "fields", "matches"} and expected is _MISSING:
        return _result(None, "missing_expected", "reference outcome is unavailable")
    if kind == "decision":
        domain = {_label(value) for value in scorer["labels"]}
        try:
            truth = _label(expected)
        except ValueError:
            return _result(None, "invalid_expected", "reference decision must be a scalar label")
        try:
            prediction = _label(output)
        except ValueError:
            return _result(0.0, "invalid_output", "decision must be a scalar label")
        if truth not in domain:
            return _result(
                None, "unknown_expected", "reference label is outside the declared domain"
            )
        return _result(
            float(truth == prediction),
            "evaluated" if prediction in domain else "unknown_label",
            "exact decision match",
            counts={
                "correct": int(truth == prediction),
                "unknown_labels": int(prediction not in domain),
            },
        )
    if kind == "multilabel":
        return _set_scores(
            expected,
            _json_value(output),
            label_domain={_label(value) for value in scorer["labels"]},
        )
    if kind == "numeric":
        low, high = scorer["minimum"], scorer["maximum"]
        if not _finite(expected) or not low <= expected <= high:
            return _result(
                None, "invalid_expected", "reference number is outside configured bounds"
            )
        if not _finite(output) or not low <= output <= high:
            return _result(0.0, "invalid_output", "output number is outside configured bounds")
        error = abs(Fraction(str(output)) - Fraction(str(expected)))
        try:
            numeric_error = float(error)
        except OverflowError:
            numeric_error = None
        return _result(
            float(error <= Fraction(str(scorer.get("tolerance", 0)))),
            "evaluated",
            "bounded numeric tolerance",
            counts={
                "absolute_error": numeric_error,
                "absolute_error_status": "available"
                if numeric_error is not None
                else "numeric_range",
            },
        )
    if kind == "fields":
        actual, truth = _json_value(output), _json_value(expected)
        if not isinstance(truth, dict) or not truth:
            return _result(
                None, "invalid_expected", "reference extraction must be a nonempty object"
            )
        if not isinstance(actual, dict):
            return _result(0.0, "invalid_output", "extraction must be an object")
        matched = sum(
            key in actual and _exactly_equal(actual[key], value) for key, value in truth.items()
        )
        extra, missing = len(set(actual) - set(truth)), len(set(truth) - set(actual))
        denominator = len(truth) + (0 if scorer.get("allow_extra", True) else extra)
        return _result(
            matched / denominator,
            "evaluated",
            "extraction field agreement",
            counts={
                "expected_fields": len(truth),
                "matched_fields": matched,
                "missing_fields": missing,
                "extra_fields": extra,
            },
        )
    if kind == "matches":
        try:
            truth = _pairs(_json_value(expected), scorer.get("symmetric", False))
        except ValueError:
            return _result(None, "invalid_expected", "reference pair identifiers must be scalars")
        try:
            actual = _pairs(_json_value(output), scorer.get("symmetric", False))
        except ValueError:
            return _result(0.0, "invalid_output", "pair identifiers must be scalars")
        if truth is None:
            return _result(None, "invalid_expected", "reference matches must be pairs")
        if actual is None:
            return _result(0.0, "invalid_output", "output matches must be pairs")
        return _set_scores(truth, actual)
    if kind == "custom":
        return _custom(output, fixture, scorer)
    legacy_fixture = {key: value for key, value in fixture.items() if key != "schema_version"}
    value = legacy_score(output, legacy_fixture, scorer)
    return _result(
        value["score"], "evaluated", f"{kind} {'passed' if value['passed'] else 'failed'}"
    )


def _output_for_side(fixture, side, trace):
    from agentloop.quality import _MISSING

    key = f"{side}_output"
    if key in fixture:
        return fixture[key]
    if side == "candidate" and "output" in fixture:
        return fixture["output"]
    metadata = getattr(trace, "metadata", {}) if trace is not None else {}
    return metadata["output"] if "output" in metadata else _MISSING


def _execution_status(fixture, side, trace):
    from agentloop.workflow_types import workflow_summary

    if trace is not None and getattr(trace, "metadata", {}).get("success") is False:
        return "failed"
    if f"{side}_status" in fixture:
        return fixture[f"{side}_status"]
    if trace is None:
        return "unreported"
    workflow = workflow_summary(getattr(trace, "metadata", {}))
    if workflow is not None:
        return workflow.get("status") or "unknown"
    return "failed" if any(event.status == "error" for event in trace.events) else "unreported"


def _side_result(output, fixture, scorer, side, trace):
    from agentloop.quality import _MISSING

    present = output is not _MISSING
    output_hash = None
    if present:
        try:
            encoded = _canonical(output)
            output_hash = sha256(encoded.encode()).hexdigest()
            output = json.loads(encoded)
        except ValueError:
            result = _result(0.0, "invalid_output", "output must be portable finite JSON")
        else:
            try:
                result = score_output(output, fixture, scorer)
            except (ValueError, TypeError, RecursionError, OverflowError) as exc:
                result = _result(
                    None,
                    "scorer_error",
                    "scorer could not assess the supplied output",
                    error_type=type(exc).__name__,
                )
    else:
        result = score_output(output, fixture, scorer)
    status = _execution_status(fixture, side, trace)
    scorer_passed = (
        result["score"] is not None
        and result["status"] == "evaluated"
        and result["score"] >= scorer.get("pass_score", 1.0)
        and result.get("custom_passed", True)
    )
    result.update(
        scorer_passed=bool(scorer_passed),
        passed=bool(scorer_passed and status in {"completed", "unreported"}),
        execution_status=status,
        output_present=present,
        output_hash=output_hash,
        output_ref=fixture.get(f"{side}_output_ref"),
    )
    return result


def _summary(results):
    known = [value["score"] for value in results if value["score"] is not None]
    failed_execution = sum(
        value["execution_status"] in {"failed", "cancelled", "interrupted", "timed_out", "stopped"}
        for value in results
    )
    unknown_execution = sum(
        value["execution_status"] in {"running", "unknown", "incomplete"} for value in results
    )
    complete = len(known) == len(results) and not failed_execution and not unknown_execution
    observed_mean = (
        float(sum(Fraction(str(value)) for value in known) / len(known)) if known else None
    )
    return {
        "case_count": len(results),
        "assessed_count": len(known),
        "unavailable_count": len(results) - len(known),
        "execution_failed_count": failed_execution,
        "execution_unknown_count": unknown_execution,
        "score": observed_mean if complete else None,
        "observed_score_mean": observed_mean,
        "complete": complete,
        "passed": all(value["passed"] for value in results),
        "status_counts": dict(sorted(Counter(value["status"] for value in results).items())),
    }


def _minimum(summary, minimum, results):
    summary["minimum_score"] = minimum
    summary["score_gate_passed"] = (
        True
        if minimum is None
        else None
        if summary["score"] is None
        else sum(Fraction(str(result["score"])) for result in results) / len(results)
        >= Fraction(str(minimum))
    )
    summary["passed"] = summary["passed"] and summary["score_gate_passed"] is True
    return summary


def _confusion(fixtures, cases, side, trace):
    from agentloop.quality import _MISSING, _json_value

    groups = {}
    for fixture, case in zip(fixtures, cases):
        scorer = fixture["scorer"]
        if scorer["type"] != "decision":
            continue
        identity = case["scorer"]["configuration_hash"]
        labels = scorer["labels"]
        if identity not in groups:
            groups[identity] = {
                "labels": labels,
                "_indices": {_label(value): index for index, value in enumerate(labels)},
                "_matrix": Counter(),
                "matrix_format": "sparse_row_column_count",
                "invalid_prediction_column": len(labels),
                "case_count": 0,
                "paired_output_count": 0,
                "missing_output_count": 0,
                "unlabelled_count": 0,
                "invalid_prediction_count": 0,
                "unassessed_output_count": 0,
            }
        group = groups[identity]
        indices = group["_indices"]
        group["case_count"] += 1
        output = _output_for_side(fixture, side, trace)
        if output is _MISSING:
            group["missing_output_count"] += 1
        expected = fixture.get("expected", _MISSING)
        try:
            row = indices.get(_label(expected)) if expected is not _MISSING else None
        except ValueError:
            row = None
        if row is None:
            group["unlabelled_count"] += 1
            continue
        if output is _MISSING:
            continue
        if case[side]["status"] in {"scorer_error", "scorer_timeout"}:
            group["unassessed_output_count"] += 1
            continue
        if "field" in scorer:
            value = _json_value(output)
            output = value.get(scorer["field"], _MISSING) if isinstance(value, dict) else _MISSING
        try:
            column = (
                indices.get(_label(output), len(labels)) if output is not _MISSING else len(labels)
            )
        except ValueError:
            column = len(labels)
        group["_matrix"][(row, column)] += 1
        group["paired_output_count"] += 1
        group["invalid_prediction_count"] += column == len(labels)
    for group in groups.values():
        matrix = group.pop("_matrix")
        group.pop("_indices")
        group["matrix"] = [[row, column, count] for (row, column), count in sorted(matrix.items())]
        row_totals, column_totals = Counter(), Counter()
        for (row, column), count in matrix.items():
            row_totals[row] += count
            column_totals[column] += count
        correct = sum(count for (row, column), count in matrix.items() if row == column)
        group["accuracy_on_available_outputs"] = (
            correct / group["paired_output_count"] if group["paired_output_count"] else None
        )
        group["coverage"] = (
            group["paired_output_count"] / group["case_count"] if group["case_count"] else None
        )
        per_label = []
        for index, label in enumerate(group["labels"]):
            tp = matrix[(index, index)]
            fp = column_totals[index] - tp
            fn = row_totals[index] - tp
            per_label.append(
                {
                    "label": label,
                    "true_positive": tp,
                    "false_positive": fp,
                    "false_negative": fn,
                    "precision": tp / (tp + fp) if tp + fp else None,
                    "recall": tp / (tp + fn) if tp + fn else None,
                    "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
                }
            )
        group["per_label"] = per_label
    return groups


def build_report(fixtures, *, baseline_trace=None, candidate_trace=None, min_score=None):
    from agentloop.quality import _MISSING, _bounded_score

    validate_fixtures(fixtures)
    if min_score is not None:
        _bounded_score(min_score, "min_score")
    fixtures = json.loads(_canonical(fixtures))
    cases = []
    for fixture in fixtures:
        scorer = fixture["scorer"]
        version = scorer.get("version", "1.0")
        configuration = {**scorer, "version": version}
        expected = fixture.get("expected", scorer.get("expected", _MISSING))
        cases.append(
            {
                "case_id": fixture["id"],
                "input_ref": fixture.get("input_ref"),
                "expected_ref": fixture.get("expected_ref"),
                "expected_present": expected is not _MISSING,
                "expected_hash": _hash(expected) if expected is not _MISSING else None,
                "scorer": {
                    "type": scorer["type"],
                    "version": version,
                    "configuration_hash": _hash(configuration),
                    "pass_score": scorer.get("pass_score", 1.0),
                    "implementation": scorer.get("callable")
                    if scorer["type"] == "custom"
                    else f"agentloop.builtin.{scorer['type']}",
                },
                **{
                    side: _side_result(
                        _output_for_side(fixture, side, trace), fixture, scorer, side, trace
                    )
                    for side, trace in (
                        ("baseline", baseline_trace),
                        ("candidate", candidate_trace),
                    )
                },
            }
        )
    for case in cases:
        case["passed"] = case["candidate"]["passed"]
    summaries = {
        side: _minimum(
            _summary([case[side] for case in cases]), min_score, [case[side] for case in cases]
        )
        for side in ("baseline", "candidate")
    }
    left, right = summaries["baseline"]["score"], summaries["candidate"]["score"]
    failed = [case for case in cases if not case["passed"]]
    report = {
        "schema_version": CONTRACT_VERSION,
        "case_count": len(cases),
        "cases": cases,
        "passed": summaries["candidate"]["passed"],
        "baseline_score": left,
        "candidate_score": right,
        "quality_delta": None
        if left is None or right is None
        else float(Fraction(str(right)) - Fraction(str(left))),
        "min_score": min_score,
        "failed_case_count": len(failed),
        "failed_cases": failed,
        "summaries": summaries,
        "indeterminate_case_count": sum(
            case["candidate"]["score"] is None
            or case["candidate"]["execution_status"] in {"running", "unknown", "incomplete"}
            for case in cases
        ),
        "trace_ids": {
            "baseline": getattr(baseline_trace, "run_id", None),
            "candidate": getattr(candidate_trace, "run_id", None),
        },
        "classification": {
            side: _confusion(fixtures, cases, side, trace)
            for side, trace in (("baseline", baseline_trace), ("candidate", candidate_trace))
        },
        "interpretation": "Versioned caller-defined criteria. Unavailable evaluations and unfinished executions cannot pass by disappearing from the denominator. Observed score means and confusion metrics do not prove task success or general correctness; classification metrics condition on available labelled outputs and disclose coverage. No raw inputs/outputs are retained by default.",
    }
    report["evidence_hash"] = _hash(report)
    return report


def score_result(output, fixture, scorer):
    owned = {**fixture, "scorer": scorer}
    validate_fixtures([owned])
    return _side_result(output, owned, scorer, "candidate", None)


def decision_span_count(trace):
    from agentloop.operations import operation_kind
    from agentloop.workflow_types import stage_summary

    return sum(
        operation_kind(event) in {"classifier", "rule"}
        or (stage_summary(event) or {}).get("kind") in {"classifier", "rule"}
        for event in trace.events
    )


def report_to_markdown(report):
    from agentloop.markdown import markdown_table_cell

    def display(value):
        return "unavailable" if value is None else str(value)

    lines = [
        "# Structured quality report",
        "",
        report["interpretation"],
        "",
        f"Contract: {report['schema_version']}; cases: {report['case_count']}; passed: {report['passed']}; indeterminate cases: {report['indeterminate_case_count']}.",
        "",
        f"Baseline score: {display(report['baseline_score'])}; candidate score: {display(report['candidate_score'])}; delta: {display(report['quality_delta'])}.",
        "",
        "| Case | Scorer / version | Baseline score / state | Candidate score / state | Candidate execution | Passed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        cells = [
            case["case_id"],
            f"{case['scorer']['type']} / {case['scorer']['version']}",
            f"{display(case['baseline']['score'])} / {case['baseline']['status']}",
            f"{display(case['candidate']['score'])} / {case['candidate']['status']}",
            case["candidate"]["execution_status"],
            str(case["passed"]),
        ]
        lines.append("| " + " | ".join(markdown_table_cell(value) for value in cells) + " |")
    for side in ("baseline", "candidate"):
        summary = report["summaries"][side]
        lines.extend(
            [
                "",
                f"## {side.title()} denominators",
                "",
                f"Assessed: {summary['assessed_count']}/{summary['case_count']}; unavailable: {summary['unavailable_count']}; failed executions: {summary['execution_failed_count']}; unknown execution: {summary['execution_unknown_count']}.",
            ]
        )
        for identity, group in report["classification"][side].items():
            lines.extend(
                [
                    "",
                    f"Decision scorer {identity}: available labelled outputs {group['paired_output_count']}/{group['case_count']}; missing outputs {group['missing_output_count']}; unlabelled {group['unlabelled_count']}; invalid predictions {group['invalid_prediction_count']}.",
                    f"Accuracy on available outputs: {display(group['accuracy_on_available_outputs'])}; coverage: {display(group['coverage'])}.",
                    "",
                    "```json",
                    json.dumps(
                        {
                            "labels": group["labels"],
                            "matrix_format": group["matrix_format"],
                            "invalid_prediction_column": group["invalid_prediction_column"],
                            "matrix": group["matrix"],
                            "per_label": group["per_label"],
                        },
                        sort_keys=True,
                        indent=2,
                    ),
                    "```",
                ]
            )
    lines.extend(
        [
            "",
            "Scorer versions, configuration hashes and expected/output hashes are preserved in JSON. Stored labels are caller-declared class names, not raw message bodies.",
            "",
        ]
    )
    return "\n".join(lines)


def attach_quality_report(trace, report, *, side="candidate"):
    if side not in {"baseline", "candidate"} or report.get("schema_version") != CONTRACT_VERSION:
        _fail("attach requires a versioned quality report and baseline/candidate side")
    copied = validate_report(report)
    digest = copied["evidence_hash"]
    identity = copied["trace_ids"][side]
    if identity is not None and identity != trace.run_id:
        _fail("quality report side belongs to a different trace")
    envelope = _side_envelope(trace.run_id, side, copied, digest)
    trace.metadata[EVIDENCE_KEY] = envelope


def _side_envelope(identity, side, copied, digest):
    envelope = {
        "schema_version": CONTRACT_VERSION,
        "report_hash": digest,
        "trace_id": identity,
        "side": side,
        "summary": copied["summaries"][side],
        "cases": [
            {
                "case_id": case["case_id"],
                "scorer": case["scorer"],
                "expected_hash": case["expected_hash"],
                "result": case[side],
            }
            for case in copied["cases"]
        ],
    }
    envelope["evidence_hash"] = _hash(envelope)
    return envelope


def validate_report(report, *, baseline_trace=None, candidate_trace=None):
    try:
        copied = json.loads(_canonical(report))
        digest = copied.pop("evidence_hash")
        if (
            copied.get("schema_version") != CONTRACT_VERSION
            or not _is_hash(digest)
            or digest != _hash(copied)
        ):
            raise ValueError
        cases = copied["cases"]
        if (
            not isinstance(cases, list)
            or not cases
            or type(copied["case_count"]) is not int
            or copied["case_count"] != len(cases)
        ):
            raise ValueError
        for side, trace in (("baseline", baseline_trace), ("candidate", candidate_trace)):
            identity = copied["trace_ids"][side]
            if identity is not None and (
                not isinstance(identity, str)
                or not identity
                or trace is not None
                and identity != trace.run_id
            ):
                raise ValueError
            identity = identity or "unbound-quality-side"
            envelope = _side_envelope(identity, side, copied, digest)
            result = read_quality_evidence(
                SimpleNamespace(run_id=identity, metadata={EVIDENCE_KEY: envelope})
            )
            if (
                result["status"] in {"invalid", "unsupported"}
                or copied[f"{side}_score"] != result["score"]
                or copied["summaries"][side]["minimum_score"] != copied["min_score"]
            ):
                raise ValueError
            score = copied[f"{side}_score"]
            if score is not None and not _finite(score):
                raise ValueError
        if any(
            type(case["passed"]) is not bool or case["passed"] != case["candidate"]["passed"]
            for case in cases
        ):
            raise ValueError
        failed = [case for case in cases if not case["passed"]]
        indeterminate = sum(
            case["candidate"]["score"] is None
            or case["candidate"]["execution_status"] in {"running", "unknown", "incomplete"}
            for case in cases
        )
        if (
            type(copied["indeterminate_case_count"]) is not int
            or copied["indeterminate_case_count"] != indeterminate
        ):
            raise ValueError
        left, right = copied["baseline_score"], copied["candidate_score"]
        delta = (
            None
            if left is None or right is None
            else float(Fraction(str(right)) - Fraction(str(left)))
        )
        if (
            copied["failed_cases"] != failed
            or type(copied["failed_case_count"]) is not int
            or copied["failed_case_count"] != len(failed)
            or copied["quality_delta"] != delta
            or type(copied["passed"]) is not bool
            or copied["passed"] != copied["summaries"]["candidate"]["passed"]
        ):
            raise ValueError
        copied["evidence_hash"] = digest
        return copied
    except (ValueError, TypeError, KeyError, RecursionError):
        _fail("structured quality report is malformed, changed, or bound to different traces")


def read_quality_evidence(trace):
    raw = getattr(trace, "metadata", {}).get(EVIDENCE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != CONTRACT_VERSION:
        return {"status": "unsupported", "score": None, "passed": False}
    try:
        copied = json.loads(_canonical(raw))
        expected = copied.pop("evidence_hash")
        if expected != _hash(copied) or copied["trace_id"] != trace.run_id:
            raise ValueError
        summary = copied["summary"]
        cases = copied["cases"]
        if not isinstance(cases, list) or not cases:
            raise ValueError
        identities = [case["case_id"] for case in cases]
        if any(not isinstance(identity, str) or not identity for identity in identities) or len(
            set(identities)
        ) != len(identities):
            raise ValueError
        unknown_statuses = {
            "missing_output",
            "missing_expected",
            "invalid_expected",
            "unknown_expected",
            "scorer_error",
            "scorer_timeout",
        }
        for case in cases:
            result, scorer = case["result"], case["scorer"]
            if (
                scorer["type"] not in _ALLOWED
                or not isinstance(scorer["version"], str)
                or not scorer["version"]
                or not _is_hash(scorer["configuration_hash"])
                or not isinstance(scorer["implementation"], str)
                or not scorer["implementation"]
            ):
                raise ValueError
            if scorer["type"] != "custom" and (
                scorer["version"] != "1.0"
                or scorer["implementation"] != f"agentloop.builtin.{scorer['type']}"
            ):
                raise ValueError
            if "custom_passed" in result and type(result["custom_passed"]) is not bool:
                raise ValueError
            if type(result["output_present"]) is not bool:
                raise ValueError
            score = result["score"]
            if score is None:
                if result["status"] not in unknown_statuses:
                    raise ValueError
            elif (
                not _finite(score)
                or not 0 <= score <= 1
                or result["status"] not in {"evaluated", "unknown_label", "invalid_output"}
            ):
                raise ValueError
            threshold = scorer["pass_score"]
            if (
                not _finite(threshold)
                or not 0 <= threshold <= 1
                or result["execution_status"] not in _EXECUTION
            ):
                raise ValueError
            passed = (
                score is not None
                and result["status"] == "evaluated"
                and score >= threshold
                and result.get("custom_passed", True)
            )
            if (
                type(result["scorer_passed"]) is not bool
                or result["scorer_passed"] != passed
                or type(result["passed"]) is not bool
                or result["passed"]
                != (passed and result["execution_status"] in {"completed", "unreported"})
            ):
                raise ValueError
        minimum = summary["minimum_score"]
        for key in (
            "case_count",
            "assessed_count",
            "unavailable_count",
            "execution_failed_count",
            "execution_unknown_count",
        ):
            if type(summary[key]) is not int or summary[key] < 0:
                raise ValueError
        if (
            summary["score_gate_passed"] is not None
            and type(summary["score_gate_passed"]) is not bool
        ):
            raise ValueError
        if minimum is not None and (not _finite(minimum) or not 0 <= minimum <= 1):
            raise ValueError
        if (
            _minimum(
                _summary([case["result"] for case in cases]),
                minimum,
                [case["result"] for case in cases],
            )
            != summary
        ):
            raise ValueError
        score = summary["score"]
        if score is not None and (not _finite(score) or not 0 <= score <= 1):
            raise ValueError
        if type(summary["passed"]) is not bool or type(summary["complete"]) is not bool:
            raise ValueError
        return {
            "status": "complete" if summary["complete"] else "incomplete",
            "score": score,
            "passed": summary["passed"],
            "report_hash": copied["report_hash"],
            "summary": summary,
            "cases": copied["cases"],
        }
    except (ValueError, KeyError, TypeError, RecursionError):
        return {"status": "invalid", "score": None, "passed": False}
