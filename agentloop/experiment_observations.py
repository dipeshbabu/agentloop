"""Bounded scalar observations attached to native experiment traces."""

from __future__ import annotations

import math
from collections import Counter

from agentloop.context_types import identifier
from agentloop.experiment_types import owned, reference
from agentloop.studies import summarize_values
from agentloop.tracer import current_trace

OBSERVATIONS_KEY = "agentloop.experiment_observations"


def observation(value, *, kind="observed", unit="count", source_ref):
    if value is not None and not (
        type(value) in {bool, int, float, str} and (not isinstance(value, str) or len(value) <= 256)
    ):
        raise ValueError("experiment observations must be bounded scalars")
    if type(value) in {int, float}:
        try:
            if not math.isfinite(value):
                raise ValueError("experiment observations must be finite")
        except OverflowError:
            raise ValueError("experiment observation is outside the numeric range") from None
    if kind not in {"observed", "estimated", "declared"}:
        raise ValueError("unknown observation kind")
    identifier(unit, "observation unit")
    reference(source_ref, "observation source")
    return {"value": value, "kind": kind, "unit": unit, "source_ref": source_ref}


def record_experiment_observations(values):
    trace = current_trace()
    if trace is None or not isinstance(trace.metadata.get("experiment_id"), str):
        raise ValueError("observations require an active experiment trace")
    if not isinstance(values, dict) or len(values) > 64:
        raise ValueError("observations require a bounded mapping")
    copied = {}
    for key, value in values.items():
        identifier(key, "observation name")
        if not isinstance(value, dict) or set(value) != {"value", "kind", "unit", "source_ref"}:
            raise ValueError("invalid observation fields")
        copied[key] = observation(**value)
    envelope = trace.metadata.setdefault(OBSERVATIONS_KEY, {"schema_version": "1.0", "values": {}})
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != "1.0"
        or not isinstance(envelope.get("values"), dict)
    ):
        raise ValueError("invalid experiment observation envelope")
    if len(set(envelope["values"]) | set(copied)) > 64:
        raise ValueError("experiment observation limit exceeded")
    if any(
        key in envelope["values"] and envelope["values"][key] != value
        for key, value in copied.items()
    ):
        raise ValueError("experiment observation cannot be rewritten")
    envelope["values"].update(owned(copied))


def summarize_observations(rows):
    sets = []
    for row in rows:
        raw = (row["trace"] or {}).get("metadata", {}).get(OBSERVATIONS_KEY)
        if raw is None:
            sets.append({})
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != "1.0"
            or not isinstance(raw.get("values"), dict)
            or len(raw["values"]) > 64
        ):
            raise ValueError("invalid saved experiment observations")
        values = {}
        for name, item in raw["values"].items():
            identifier(name, "observation name")
            values[name] = observation(**item)
        sets.append(values)
    result = {}
    for name in sorted({key for values in sets for key in values}):
        available = [
            values[name] for values in sets if name in values and values[name]["value"] is not None
        ]
        kinds = sorted({item["kind"] for item in available})
        units = sorted({item["unit"] for item in available})
        sources = sorted({item["source_ref"] for item in available})
        row_values = [values.get(name, {}).get("value") for values in sets]
        compatible = len(kinds) == len(units) == len(sources) == 1
        summary = {
            "planned_count": len(rows),
            "known_count": len(available),
            "missing_count": len(rows) - len(available),
            "kinds": kinds,
            "units": units,
            "source_refs": sources,
            "aggregation_status": "compatible" if compatible else "unknown_or_mixed_basis",
        }
        if compatible and all(type(value) is bool for value in row_values if value is not None):
            true_count = sum(value is True for value in row_values)
            quality_true = sum(
                value is True and row["receipt"] is not None and row["receipt"]["quality_passed"]
                for value, row in zip(row_values, rows)
            )
            summary.update(
                true_count=true_count,
                false_count=sum(value is False for value in row_values),
                true_fraction_of_planned=true_count / len(rows),
                quality_preserving_true_count=quality_true,
                quality_preserving_true_fraction_of_planned=quality_true / len(rows),
            )
        elif compatible and all(
            type(value) in {int, float} for value in row_values if value is not None
        ):
            summary["values"] = summarize_values(row_values)
            summary["quality_preserving_values"] = summarize_values(
                [
                    value
                    if row["receipt"] is not None and row["receipt"]["quality_passed"]
                    else None
                    for value, row in zip(row_values, rows)
                ]
            )
        elif compatible:
            summary["value_counts"] = dict(
                sorted(Counter(str(value) for value in row_values if value is not None).items())
            )
        result[name] = summary
    return result
