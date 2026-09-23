"""Read-only validation and summaries of frozen substitution trial bundles."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from fractions import Fraction
from statistics import fmean

from agentloop.judgment_types import JudgeIdentity, canonical, fingerprint, finite, is_hash, text
from agentloop.quality import SUPPORTED_SCORER_TYPES
from agentloop.structured_quality import validate_report
from agentloop.substitution_evidence import read_trial_evidence
from agentloop.substitution_types import SUBSTITUTION_VERSION, DecisionStep
from agentloop.tracer import AgentTrace


def _copy(value):
    return json.loads(canonical(value))


def load_bundle(bundle, max_invocations=10000):
    owned = _copy(bundle)
    try:
        if owned["schema_version"] != SUBSTITUTION_VERSION or type(owned["enabled"]) is not bool:
            raise ValueError("unsupported substitution bundle")
        protocol, declarations = owned["protocol"], owned["implementations"]
        if (
            protocol["schema_version"] != SUBSTITUTION_VERSION
            or not isinstance(declarations, dict)
            or len(declarations) < 2
            or owned["baseline"] not in declarations
        ):
            raise ValueError("invalid experiment plan")
        if owned["plan_hash"] != fingerprint(
            {"protocol": protocol, "baseline": owned["baseline"], "implementations": declarations}
        ):
            raise ValueError("experiment plan hash mismatch")
        for key in ("name", "version"):
            text(protocol[key], key)
        step = DecisionStep(**protocol["step"])
        if type(protocol["synthetic"]) is not bool or step.synthetic and not protocol["synthetic"]:
            raise ValueError("synthetic source markers must be preserved")
        for name, declaration in declarations.items():
            text(name, "condition")
            if declaration["name"] != name or declaration["kind"] not in {
                "model",
                "classifier",
                "rule",
                "tool",
                "external_service",
            }:
                raise ValueError("invalid implementation declaration")
            JudgeIdentity(**declaration["identity"])
        examples = {}
        for item in protocol["examples"]:
            for key in ("example_id", "input_ref", "quality_ref"):
                text(item[key], key)
            if (
                item["example_id"] in examples
                or not is_hash(item["input_hash"])
                or item["expected_hash"] is not None
                and not is_hash(item["expected_hash"])
            ):
                raise ValueError("invalid or duplicate example declaration")
            examples[item["example_id"]] = item
        if (
            not examples
            or type(protocol["repetitions"]) is not int
            or protocol["repetitions"] < 1
            or type(protocol["seed"]) is not int
        ):
            raise ValueError("invalid examples, repetitions or seed")
        for key in ("min_quality", "max_quality_drop"):
            if not finite(protocol[key]) or not 0 <= protocol[key] <= 1:
                raise ValueError("invalid quality threshold")
        if protocol["timeout_s"] is not None and (
            not finite(protocol["timeout_s"]) or protocol["timeout_s"] <= 0
        ):
            raise ValueError("invalid timeout")
        scorer = protocol["scorer"]
        text(scorer["version"], "scorer version")
        if scorer["type"] not in SUPPORTED_SCORER_TYPES or not is_hash(scorer["config_hash"]):
            raise ValueError("invalid scorer identity")
        count = len(examples) * protocol["repetitions"] * len(declarations)
        if type(max_invocations) is not int or max_invocations < 1 or count > max_invocations:
            raise ValueError("planned experiment exceeds max_invocations")
        if (
            not isinstance(owned["records"], list)
            or not isinstance(owned["pairs"], list)
            or not isinstance(owned["environment"], dict)
        ):
            raise ValueError("invalid record collections or environment")
        if set(owned["environment"]) != {"python", "system", "architecture"} or any(
            not isinstance(value, str) for value in owned["environment"].values()
        ):
            raise ValueError("invalid environment declaration")
        return owned, examples
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid substitution bundle") from None


def index_trials(bundle, examples):
    indexed, extras = defaultdict(list), []
    for index, row in enumerate(bundle["records"]):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("condition"), str)
            or row["condition"] not in bundle["implementations"]
            or not isinstance(row.get("example_id"), str)
            or row["example_id"] not in examples
            or type(row.get("repetition")) is not int
            or not 0 <= row["repetition"] < bundle["protocol"]["repetitions"]
        ):
            extras.append(index)
        else:
            indexed[row["condition"], row["example_id"], row["repetition"]].append(row)
    slots, run_ids = {}, Counter()
    for rows in indexed.values():
        for row in rows:
            if isinstance(row.get("trace"), dict) and isinstance(row["trace"].get("run_id"), str):
                run_ids[row["trace"]["run_id"]] += 1
    for name, declaration in bundle["implementations"].items():
        for example_id, example in examples.items():
            for repeat in range(bundle["protocol"]["repetitions"]):
                key = name, example_id, repeat
                rows = indexed.get(key, [])
                slot = {
                    "condition": name,
                    "example_id": example_id,
                    "repetition": repeat,
                    "status": "missing",
                    "trace": None,
                    "receipt": None,
                }
                if len(rows) > 1:
                    slot["status"] = "duplicate"
                elif rows:
                    try:
                        trace = AgentTrace.from_dict(_copy(rows[0]["trace"]))
                        receipt = read_trial_evidence(trace)
                        if (
                            receipt is None
                            or receipt["status"] != "valid"
                            or receipt["plan_hash"] != bundle["plan_hash"]
                            or receipt["example_id"] != example_id
                            or receipt["repetition"] != repeat
                            or receipt["implementation"] != declaration
                            or receipt["input_hash"] != example["input_hash"]
                            or run_ids[trace.run_id] != 1
                            or (not bundle["enabled"] and receipt["outcome"] != "disabled")
                            or trace.metadata.get("plan_hash") != bundle["plan_hash"]
                            or trace.metadata.get("example_id") != example_id
                            or trace.metadata.get("repetition") != repeat
                        ):
                            raise ValueError("trial does not match frozen plan")
                        if trace.metadata.get("synthetic") != bundle["protocol"]["synthetic"]:
                            raise ValueError("trial changed the synthetic marker")
                        slot.update(status=receipt["outcome"], trace=trace, receipt=receipt)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        slot["status"] = "invalid_record"
                slots[key] = slot
    return slots, extras


def _quality_matches(quality, before, after, example, protocol):
    checked = validate_report(
        quality, baseline_trace=before["trace"], candidate_trace=after["trace"]
    )
    if (
        checked["trace_ids"]
        != {"baseline": before["trace"].run_id, "candidate": after["trace"].run_id}
        or checked["case_count"] != 1
        or checked["min_score"] != protocol["min_quality"]
    ):
        raise ValueError("quality is not bound to this trial pair")
    case = checked["cases"][0]
    scorer = case["scorer"]
    if (
        case["case_id"] != example["example_id"]
        or case["input_ref"] != example["input_ref"]
        or case["expected_ref"] != example["quality_ref"]
        or case["expected_hash"] != example["expected_hash"]
        or scorer["type"] != protocol["scorer"]["type"]
        or scorer["version"] != protocol["scorer"]["version"]
        or scorer["configuration_hash"] != protocol["scorer"]["config_hash"]
    ):
        raise ValueError("quality changed the frozen example or scorer")
    for side, trial in (("baseline", before), ("candidate", after)):
        if case[side]["output_hash"] != trial["receipt"]["output_hash"] or case[side][
            "output_present"
        ] != (trial["status"] == "completed"):
            raise ValueError("quality output does not match the recorded result")
        if trial["status"] != "completed" and checked[f"{side}_score"] is not None:
            raise ValueError("unavailable output cannot have known quality")
    return checked


def paired_assessments(bundle, slots, examples):
    indexed, extras = defaultdict(list), []
    candidates = set(bundle["implementations"]) - {bundle["baseline"]}
    for index, row in enumerate(bundle["pairs"]):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("condition"), str)
            or row["condition"] not in candidates
            or not isinstance(row.get("example_id"), str)
            or row["example_id"] not in examples
            or type(row.get("repetition")) is not int
            or not 0 <= row["repetition"] < bundle["protocol"]["repetitions"]
        ):
            extras.append(index)
        else:
            indexed[row["condition"], row["example_id"], row["repetition"]].append(row)
    result = []
    for name in sorted(candidates):
        for example_id, example in examples.items():
            for repeat in range(bundle["protocol"]["repetitions"]):
                key = name, example_id, repeat
                before, after = slots[bundle["baseline"], example_id, repeat], slots[key]
                row = {
                    "condition": name,
                    "example_id": example_id,
                    "repetition": repeat,
                    "status": "unavailable",
                    "baseline_score": None,
                    "candidate_score": None,
                    "candidate_passed": None,
                    "baseline_agreement": None,
                    "quality": None,
                }
                saved = indexed.get(key, [])
                if len(saved) == 1 and before["trace"] is not None and after["trace"] is not None:
                    pair = saved[0]
                    if (
                        pair.get("baseline_run_id") == before["trace"].run_id
                        and pair.get("candidate_run_id") == after["trace"].run_id
                    ):
                        if before["status"] == after["status"] == "completed":
                            row["baseline_agreement"] = (
                                before["receipt"]["output_hash"] == after["receipt"]["output_hash"]
                            )
                        if pair.get("quality") is not None:
                            try:
                                quality = _quality_matches(
                                    pair["quality"], before, after, example, bundle["protocol"]
                                )
                                row.update(
                                    status="assessed",
                                    baseline_score=quality["baseline_score"],
                                    candidate_score=quality["candidate_score"],
                                    candidate_passed=quality["summaries"]["candidate"]["passed"],
                                    quality=quality,
                                )
                            except (ValueError, TypeError, KeyError):
                                row["status"] = "invalid_quality"
                        elif pair.get("quality_error") == "assessment_failed":
                            row["status"] = "scoring_failed"
                elif len(saved) > 1:
                    row["status"] = "duplicate_quality"
                result.append(row)
    return result, extras


def _mean(values):
    if not values:
        return None
    try:
        return fmean(values)
    except OverflowError:
        return float(sum((Fraction(str(value)) for value in values), Fraction()) / len(values))


def _exact_mean(values):
    return (
        sum((Fraction(str(value)) for value in values), Fraction()) / len(values)
        if values
        else None
    )


def summarize_substitution_experiment(bundle, *, max_invocations=10000):
    owned, examples = load_bundle(bundle, max_invocations)
    slots, unmatched = index_trials(owned, examples)
    pairs, unmatched_pairs = paired_assessments(owned, slots, examples)
    by_condition, comparisons_by_condition = defaultdict(list), defaultdict(list)
    for row in slots.values():
        by_condition[row["condition"]].append(row)
    for row in pairs:
        comparisons_by_condition[row["condition"]].append(row)
    results = []
    for name, declaration in owned["implementations"].items():
        rows = by_condition[name]
        counts = Counter(item["status"] for item in rows)
        receipts = [item["receipt"] for item in rows if item["receipt"] is not None]
        metrics = {}
        for key in ("latency_ms", "input_tokens", "output_tokens", "cost_usd"):
            values = [
                item["latency_ms"] if key == "latency_ms" else item["usage"][key]
                for item in receipts
            ]
            known = [value for value in values if value is not None]
            metrics[key] = {
                "observed_mean": _mean(known),
                "observed_count": len(known),
                "missing_count": len(rows) - len(known),
                "complete": len(known) == len(rows),
            }
        results.append(
            {
                **declaration,
                "planned": len(rows),
                "status_counts": dict(counts),
                "failure_rate": sum(
                    counts[key]
                    for key in ("failed", "timed_out", "invalid_result", "invalid_record")
                )
                / len(rows),
                "unavailable_output_rate": (len(rows) - counts["completed"]) / len(rows),
                "measurements": metrics,
                "cost_basis_counts": dict(
                    Counter(item["usage"]["cost_basis"] for item in receipts)
                ),
                "token_basis_counts": dict(
                    Counter(item["usage"]["token_basis"] for item in receipts)
                ),
            }
        )
    comparisons = []
    for name, values in comparisons_by_condition.items():
        by_example = defaultdict(list)
        for item in values:
            by_example[item["example_id"]].append(item)
        complete_examples = [
            rows
            for rows in by_example.values()
            if len(rows) == owned["protocol"]["repetitions"]
            and all(
                item["baseline_score"] is not None and item["candidate_score"] is not None
                for item in rows
            )
        ]
        complete = len(complete_examples) == len(examples)
        before = _exact_mean(
            [_exact_mean([item["baseline_score"] for item in rows]) for rows in complete_examples]
        )
        after = _exact_mean(
            [_exact_mean([item["candidate_score"] for item in rows]) for rows in complete_examples]
        )
        agreement = [
            float(item["baseline_agreement"])
            for item in values
            if item["baseline_agreement"] is not None
        ]
        preserved = None
        if complete:
            preserved = (
                all(item["candidate_passed"] for item in values)
                and after >= Fraction(str(owned["protocol"]["min_quality"]))
                and before - after <= Fraction(str(owned["protocol"]["max_quality_drop"]))
            )
        comparisons.append(
            {
                "condition": name,
                "planned_pairs": len(values),
                "complete_quality_examples": len(complete_examples),
                "quality_complete": complete,
                "quality_status_counts": dict(Counter(item["status"] for item in values)),
                "observed_baseline_quality": float(before) if before is not None else None,
                "observed_candidate_quality": float(after) if after is not None else None,
                "observed_quality_delta": float(after - before)
                if before is not None and after is not None
                else None,
                "quality_preserved_on_examples": preserved,
                "weighting": "equal examples; all planned repetitions must have known quality",
                "baseline_agreement": {
                    "method": "typed_canonical_json_equality",
                    "observed_rate": _mean(agreement),
                    "observed_count": len(agreement),
                    "missing_count": len(values) - len(agreement),
                    "is_independent_quality": False,
                },
            }
        )
    return {
        "schema_version": SUBSTITUTION_VERSION,
        "name": owned["protocol"]["name"],
        "plan_hash": owned["plan_hash"],
        "source_bundle_hash": fingerprint(owned),
        "baseline": owned["baseline"],
        "synthetic": owned["protocol"]["synthetic"],
        "environment": owned["environment"],
        "example_count": len(examples),
        "unique_input_count": len({item["input_hash"] for item in examples.values()}),
        "sample_status": "exploratory",
        "interpretation": "Exploratory offline comparison on frozen examples; baseline agreement is not independent quality and no candidate is activated. Small samples and shared baseline observations cannot establish general model capability.",
        "measurement_scope": "declared_decision_step",
        "implementations": results,
        "comparisons": comparisons,
        "pairs": pairs,
        "unmatched_records": unmatched,
        "unmatched_pairs": unmatched_pairs,
        "trial_statuses": [
            {key: row[key] for key in ("condition", "example_id", "repetition", "status")}
            for row in slots.values()
        ],
    }
