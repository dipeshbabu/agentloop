"""Frozen, label-blind backend comparisons with explicit missing-case accounting."""

from __future__ import annotations

import json
import platform
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from fractions import Fraction
from itertools import combinations
from statistics import fmean

from agentloop.judgment_benchmark_types import BENCHMARK_VERSION, JudgmentBenchmark
from agentloop.judgment_types import JudgeIdentity, JudgmentBackend, canonical, fingerprint, text
from agentloop.judgments import JudgmentSession, validate_judgment_record
from agentloop.markdown import markdown_table_cell, markdown_text

_CATEGORIES = {"deterministic", "local_predictor", "llm", "typed_service"}
_AVAILABILITY = {"ready", "not_configured", "missing_credentials"}


@dataclass(frozen=True)
class BenchmarkBackend:
    name: str
    backend: JudgmentBackend
    category: str
    simulated: bool = False

    def __post_init__(self):
        text(self.name, "backend name")
        if self.category not in _CATEGORIES or type(self.simulated) is not bool:
            raise ValueError("backend requires a supported category and boolean simulated flag")
        if type(self.backend.identity) is not JudgeIdentity or not callable(self.backend.judge):
            raise ValueError("backend must implement JudgmentBackend")

    def declaration(self):
        return {
            "name": self.name,
            "category": self.category,
            "simulated": self.simulated,
            "judge": asdict(self.backend.identity),
        }


def _copy(value):
    return json.loads(canonical(value))


def _availability(backend):
    value = getattr(backend, "availability", "ready")
    if value not in _AVAILABILITY:
        raise ValueError("unsupported backend availability declaration")
    return value


def run_judgment_benchmark(
    protocol: JudgmentBenchmark, backends, *, enabled=False, max_invocations=10000
):
    """Execute a finite schedule; imported artifacts never configure executable code.

    Backends are explicit host-owned objects. No retries, warmups, cache hits or
    production routing are hidden in the benchmark. This bounds call count, not
    provider spend; the host must authorize and bound any remote transport.
    """
    if type(protocol) is not JudgmentBenchmark or type(enabled) is not bool:
        raise ValueError("benchmark requires a typed protocol and boolean enabled flag")
    if (
        not isinstance(backends, (list, tuple))
        or not backends
        or any(type(item) is not BenchmarkBackend for item in backends)
    ):
        raise ValueError("backends must be a nonempty list or tuple of BenchmarkBackend")
    names = [item.name for item in backends]
    if len(set(names)) != len(names):
        raise ValueError("backend names must be unique")
    count = len(backends) * len(protocol.examples) * protocol.repetitions
    if type(max_invocations) is not int or max_invocations < 1 or count > max_invocations:
        raise ValueError("planned benchmark exceeds max_invocations")
    configurations = {item.name: item for item in backends}
    declarations = [configurations[name].declaration() for name in sorted(names)]
    plan = {"protocol": protocol.to_dict(), "backends": declarations}
    sessions = {name: JudgmentSession(configurations[name].backend, cache_size=0) for name in names}
    jobs = [
        (name, index, repeat)
        for name in sorted(names)
        for index in range(len(protocol.examples))
        for repeat in range(protocol.repetitions)
    ]
    random.Random(protocol.seed).shuffle(jobs)  # nosec B311
    rows = []
    for ordinal, (name, index, repeat) in enumerate(jobs):
        example = protocol.examples[index]
        row = {
            "ordinal": ordinal,
            "backend": name,
            "example_id": example.example_id,
            "repetition": repeat,
            "availability": "ready",
            "receipt": None,
        }
        try:
            row["availability"] = _availability(configurations[name].backend)
            # Expected answers and label references are never passed to a backend.
            row["receipt"] = sessions[name].evaluate(
                example.request, enabled=enabled, timeout_s=protocol.timeout_s
            )
            row["status"] = "recorded"
        except Exception:
            row["status"] = "execution_error"
        rows.append(row)
    return {
        "schema_version": BENCHMARK_VERSION,
        **plan,
        "plan_hash": fingerprint(plan),
        "enabled": enabled,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "architecture": platform.machine(),
        },
        "records": rows,
    }


def _load_bundle(bundle):
    owned = _copy(bundle)
    try:
        if owned["schema_version"] != BENCHMARK_VERSION or type(owned["enabled"]) is not bool:
            raise ValueError("unsupported benchmark bundle")
        protocol = JudgmentBenchmark.from_dict(owned["protocol"])
        declarations = owned["backends"]
        if not isinstance(declarations, list) or not declarations:
            raise ValueError("benchmark requires backend declarations")
        for item in declarations:
            text(item["name"], "backend name")
            JudgeIdentity(**item["judge"])
            if item["category"] not in _CATEGORIES or type(item["simulated"]) is not bool:
                raise ValueError("invalid backend declaration")
        if len({item["name"] for item in declarations}) != len(declarations):
            raise ValueError("duplicate backend declarations")
        if owned["plan_hash"] != fingerprint(
            {"protocol": protocol.to_dict(), "backends": declarations}
        ):
            raise ValueError("benchmark plan hash mismatch")
        if not isinstance(owned["records"], list):
            raise ValueError("benchmark records must be a list")
        environment = owned["environment"]
        if (
            not isinstance(environment, dict)
            or set(environment) != {"python", "implementation", "system", "architecture"}
            or any(not isinstance(value, str) for value in environment.values())
        ):
            raise ValueError("invalid benchmark environment")
        return owned, protocol, {item["name"]: item for item in declarations}
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid judgment benchmark bundle") from None


def _slots(bundle, protocol, declarations):
    examples = {item.example_id: item for item in protocol.examples}
    indexed, unmatched = defaultdict(list), []
    for index, row in enumerate(bundle["records"]):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("backend"), str)
            or row["backend"] not in declarations
            or not isinstance(row.get("example_id"), str)
            or row["example_id"] not in examples
            or type(row.get("repetition")) is not int
            or not 0 <= row["repetition"] < protocol.repetitions
        ):
            unmatched.append({"record_index": index, "reason": "unmatched_record"})
            continue
        indexed[(row["backend"], row["example_id"], row["repetition"])].append(row)
    slots = {}
    invocation_ids = Counter()
    for rows in indexed.values():
        for row in rows:
            invocation = (
                row.get("receipt", {}).get("invocation", {})
                if isinstance(row.get("receipt"), dict)
                else {}
            )
            if isinstance(invocation, dict) and isinstance(invocation.get("id"), str):
                invocation_ids[invocation["id"]] += 1
    for name, declaration in declarations.items():
        for example in protocol.examples:
            for repeat in range(protocol.repetitions):
                key = name, example.example_id, repeat
                rows = indexed.get(key, [])
                slot = {
                    "backend": name,
                    "example_id": example.example_id,
                    "repetition": repeat,
                    "status": "missing",
                    "value": None,
                    "receipt": None,
                    "availability": "unknown",
                }
                if len(rows) > 1:
                    slot["status"] = "duplicate"
                elif rows:
                    row = rows[0]
                    try:
                        if row.get("availability") not in _AVAILABILITY:
                            raise ValueError("invalid availability")
                        slot["availability"] = row["availability"]
                        if row.get("status") == "execution_error" and row.get("receipt") is None:
                            slot["status"] = "error"
                        elif row.get("status") == "recorded":
                            receipt = validate_judgment_record(row["receipt"])
                            if (
                                receipt["request"] != example.request.to_dict()
                                or receipt["judge"] != declaration["judge"]
                                or receipt["invocation"]["cache_hit"]
                                or invocation_ids[receipt["invocation"]["id"]] != 1
                                or (not bundle["enabled"] and receipt["invocation"]["dispatched"])
                            ):
                                raise ValueError("receipt does not match its frozen benchmark slot")
                            result = receipt["evaluation"]
                            slot.update(
                                status=result["status"], value=result["value"], receipt=receipt
                            )
                        else:
                            raise ValueError("invalid record status")
                    except (ValueError, TypeError, KeyError, AttributeError):
                        slot["status"] = "invalid_record"
                slots[key] = slot
    return slots, unmatched


def _mean(values):
    if not values:
        return None
    try:
        return fmean(values)
    except OverflowError:
        return float(sum((Fraction(str(item)) for item in values), Fraction()) / len(values))


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else _mean(ordered[middle - 1 : middle + 1])


def _normalized_difference(left, right, low, high):
    return float(
        abs(Fraction(str(left)) - Fraction(str(right))) / (Fraction(str(high)) - Fraction(str(low)))
    )


def _calibration(cases):
    """Descriptive probability calibration of per-example mean forecasts."""
    if len(cases) < 20 or len({label for _, label in cases}) < 2:
        return {
            "status": "insufficient_support",
            "example_count": len(cases),
            "minimum_examples": 20,
            "expected_calibration_error": None,
            "bins": [],
            "calibrated": False,
        }
    bins = [[] for _ in range(10)]
    for probability, label in cases:
        bins[min(int(probability * 10), 9)].append((probability, label))
    summaries, error = [], 0.0
    for index, values in enumerate(bins):
        forecast = _mean([value for value, _ in values])
        frequency = _mean([int(label) for _, label in values])
        if values:
            error += len(values) / len(cases) * abs(forecast - frequency)
        summaries.append(
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": len(values),
                "mean_probability": forecast,
                "positive_frequency": frequency,
            }
        )
    return {
        "status": "descriptive",
        "example_count": len(cases),
        "expected_calibration_error": error,
        "bins": summaries,
        "calibrated": False,
        "basis": "one mean probability per completely observed independently labelled example",
    }


def _quality(protocol, name, slots):
    groups = defaultdict(list)
    for example in protocol.examples:
        groups[fingerprint(asdict(example.request.spec))].append(example)
    results = []
    for key, examples in groups.items():
        spec = examples[0].request.spec
        labelled = [item for item in examples if item.label_status == "independent"]
        scores, probabilities = [], []
        for example in labelled:
            values = [
                slots[name, example.example_id, repeat] for repeat in range(protocol.repetitions)
            ]
            if any(item["status"] != "known" for item in values):
                continue
            answers = [item["value"] for item in values]
            if spec.kind == "probability":
                scores.append(fmean((value - int(example.expected)) ** 2 for value in answers))
                probabilities.append((fmean(answers), example.expected))
            elif spec.kind == "score":
                scores.append(
                    fmean(
                        _normalized_difference(value, example.expected, spec.minimum, spec.maximum)
                        for value in answers
                    )
                )
            else:
                scores.append(fmean(float(value == example.expected) for value in answers))
        metric = {"probability": "brier_loss", "score": "normalized_absolute_error"}.get(
            spec.kind, "accuracy"
        )
        complete = len(scores) == len(examples) and bool(scores)
        result = {
            "cohort_id": key,
            "spec": asdict(spec),
            "planned_examples": len(examples),
            "independently_labelled_examples": len(labelled),
            "complete_labelled_examples": len(scores),
            "unlabelled_examples": len(examples) - len(labelled),
            "incomplete_labelled_examples": len(labelled) - len(scores),
            "coverage": len(scores) / len(examples),
            "complete": complete,
            "metric": metric,
            "value": _mean(scores) if complete else None,
            "observed_value": _mean(scores),
            "better": "higher" if metric == "accuracy" else "lower",
            "weighting": "equal examples; mean across planned repetitions only when all are known",
            "label_versions": sorted({item.label_version for item in labelled}),
        }
        if spec.kind == "probability":
            result["calibration"] = {**_calibration(probabilities), "full_coverage": complete}
        results.append(result)
    return results


def _performance(values):
    timings, costs, inputs, outputs = [], [], [], []
    cost_bases, token_bases = Counter(), Counter()
    for slot in values:
        if slot["receipt"] is None:
            continue
        invocation = slot["receipt"]["invocation"]
        if invocation["dispatched"]:
            timings.append(invocation["latency_ms"])
        usage = invocation["usage"]
        for key, target in (
            ("cost_usd", costs),
            ("input_tokens", inputs),
            ("output_tokens", outputs),
        ):
            if usage[key] is not None:
                target.append(usage[key])
        cost_bases[usage["cost_basis"]] += 1
        token_bases[usage["token_basis"]] += 1

    def total(items):
        # Exact decimal accumulation avoids an intermediate overflow or a large
        # sum silently becoming infinity in portable JSON.
        value = sum((Fraction(str(item)) for item in items), Fraction())
        try:
            return float(value)
        except OverflowError:
            return None

    known_cost = total(costs)
    return {
        "latency_ms": {
            "mean": _mean(timings),
            "median": _median(timings),
            "observed_invocations": len(timings),
            "planned_invocations": len(values),
        },
        "cost": {
            "known_usd": known_cost,
            "complete": len(costs) == len(values) and known_cost is not None,
            "aggregation_status": "out_of_range" if known_cost is None else "finite",
            "observed_invocations": len(costs),
            "basis_counts": dict(cost_bases),
        },
        "tokens": {
            "known_input": sum(inputs),
            "known_output": sum(outputs),
            "input_complete": len(inputs) == len(values),
            "output_complete": len(outputs) == len(values),
            "basis_counts": dict(token_bases),
        },
        "interpretation": "Observed offline invocation overhead; unknown/missing measurements are not zero. No cached invocations are admitted.",
    }


def _disagreements(protocol, declarations, slots):
    result = []
    cohort_ids = {
        example.example_id: fingerprint(asdict(example.request.spec))
        for example in protocol.examples
    }
    for left, right in combinations(sorted(declarations), 2):
        groups = defaultdict(list)
        for example in protocol.examples:
            spec = example.request.spec
            for repeat in range(protocol.repetitions):
                a, b = (
                    slots[left, example.example_id, repeat],
                    slots[right, example.example_id, repeat],
                )
                pair = {
                    "example_id": example.example_id,
                    "repetition": repeat,
                    "status": "unavailable",
                    "left": None,
                    "right": None,
                    "difference": None,
                }
                if a["status"] == b["status"] == "known":
                    pair.update(status="observed", left=a["value"], right=b["value"])
                    if spec.kind in {"boolean", "choice"}:
                        pair["difference"] = float(a["value"] != b["value"])
                    else:
                        low, high = (
                            (0, 1) if spec.kind == "probability" else (spec.minimum, spec.maximum)
                        )
                        pair["difference"] = _normalized_difference(
                            a["value"], b["value"], low, high
                        )
                groups[cohort_ids[example.example_id]].append(pair)
        for key, pairs in groups.items():
            observed = [item["difference"] for item in pairs if item["status"] == "observed"]
            result.append(
                {
                    "left_backend": left,
                    "right_backend": right,
                    "cohort_id": key,
                    "planned_pairs": len(pairs),
                    "observed_pairs": len(observed),
                    "observed_mean_difference": _mean(observed),
                    "pairs": pairs,
                    "interpretation": "Disagreement is not independent correctness evidence.",
                }
            )
    return result


def summarize_judgment_benchmark(bundle, *, max_invocations=10000, max_pairwise_comparisons=100000):
    """Analyze saved receipts without importing, configuring or executing a backend."""
    owned, protocol, declarations = _load_bundle(bundle)
    planned = len(protocol.examples) * protocol.repetitions * len(declarations)
    if type(max_invocations) is not int or max_invocations < 1 or planned > max_invocations:
        raise ValueError("planned benchmark exceeds max_invocations")
    pairwise = (
        len(declarations)
        * (len(declarations) - 1)
        // 2
        * len(protocol.examples)
        * protocol.repetitions
    )
    if (
        type(max_pairwise_comparisons) is not int
        or max_pairwise_comparisons < 0
        or pairwise > max_pairwise_comparisons
    ):
        raise ValueError("planned benchmark exceeds max_pairwise_comparisons")
    slots, unmatched = _slots(owned, protocol, declarations)
    summaries = []
    by_backend = defaultdict(list)
    for item in slots.values():
        by_backend[item["backend"]].append(item)
    for name, declaration in declarations.items():
        values = by_backend[name]
        counts = Counter(item["status"] for item in values)
        availability = Counter(item["availability"] for item in values)
        denominator = len(values)
        summaries.append(
            {
                **declaration,
                "planned_invocations": denominator,
                "status_counts": dict(counts),
                "known_rate": counts["known"] / denominator,
                "unavailable_rate": (denominator - counts["known"]) / denominator,
                "failure_rate": sum(
                    counts[key] for key in ("error", "invalid_result", "invalid_record")
                )
                / denominator,
                "timeout_rate": counts["timeout"] / denominator,
                "abstention_rate": counts["unknown"] / denominator,
                "availability_counts": dict(availability),
                "quality": _quality(protocol, name, slots),
                "performance": _performance(values),
            }
        )
    return {
        "schema_version": BENCHMARK_VERSION,
        "name": protocol.name,
        "version": protocol.version,
        "plan_hash": owned["plan_hash"],
        "protocol_hash": protocol.protocol_hash,
        "source_bundle_hash": fingerprint(owned),
        "environment": owned["environment"],
        "synthetic": protocol.synthetic,
        "simulated_backends": [item["name"] for item in declarations.values() if item["simulated"]],
        "enabled": owned["enabled"],
        "planned_invocations": len(slots),
        "record_count": len(owned["records"]),
        "unmatched_records": unmatched,
        "backends": summaries,
        "disagreements": _disagreements(protocol, declarations, slots),
        "cases": list(slots.values()),
        "interpretation": "Descriptive comparison on frozen examples and caller-declared independent labels. Agreement is not ground truth; no backend ranking or production substitution is authorized.",
    }


def judgment_benchmark_markdown(report):
    lines = [
        f"# Judgment benchmark: {markdown_text(report['name'])}",
        "",
        report["interpretation"],
        "",
    ]
    if report["synthetic"] or report["simulated_backends"]:
        lines.extend(
            [
                "Synthetic inputs or simulated backends are present. These results do not establish real-model performance or empirical optimization gains.",
                "",
            ]
        )
    lines.extend(
        [
            "## Availability and performance",
            "",
            "| Backend | Planned | Known | Failure | Timeout | Abstention | Known cost USD | Cost complete | Observed latency ms |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for backend in report["backends"]:
        cells = [
            backend["name"],
            backend["planned_invocations"],
            backend["known_rate"],
            backend["failure_rate"],
            backend["timeout_rate"],
            backend["abstention_rate"],
            backend["performance"]["cost"]["known_usd"],
            backend["performance"]["cost"]["complete"],
            backend["performance"]["latency_ms"]["mean"],
        ]
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell("unavailable" if item is None else str(item)) for item in cells
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Independent-label quality",
            "",
            "Full metric values require complete labelled coverage. Observed values are conditional on the completely observed labelled examples; coverage and failures remain part of the comparison.",
            "",
            "| Backend | Question | Metric | Full value | Observed value | Coverage | Calibration |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for backend in report["backends"]:
        for cohort in backend["quality"]:
            cells = [
                backend["name"],
                cohort["spec"]["question"],
                cohort["metric"],
                cohort["value"],
                cohort["observed_value"],
                cohort["coverage"],
                cohort.get("calibration", {}).get("status", "not applicable"),
            ]
            lines.append(
                "| "
                + " | ".join(
                    markdown_table_cell("unavailable" if item is None else str(item))
                    for item in cells
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## Backend disagreement",
            "",
            "Agreement with another backend is not independent correctness evidence. Pairwise counts, differences, unavailable pairs and original receipts are retained in the JSON report.",
            "",
        ]
    )
    return "\n".join(lines)
