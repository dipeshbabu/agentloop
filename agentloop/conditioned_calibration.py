"""Workload-conditioned calibration of intervention predictions (issue #403).

Every pass predicts a gain. After an intervention is measured, the prediction
can be compared with what happened. This module learns, per stratum, how far
predictions tend to be off, and reports how much that helps on task clusters it
did not learn from.

An observation is one measured (or unmeasured) intervention:

```python
{
    "pass": "parallelize_tools",  # required
    "task_cluster": "retrieval",  # required: the unit for held-out splits
    "model": "gpt-x",
    "framework": "langgraph",
    "environment": "ci",  # optional
    "predicted": 1200.0,  # the pass's predicted gain, > 0
    "measured": 800.0,  # the measured gain; may be negative
    "measured_basis": "observed",  # observed | estimated | missing
    "success": True,  # did the intervention keep quality?
    "confounders": ["provider outage"],  # anything that could explain the result
    "observed_at": "2026-11-01T00:00:00+00:00",
}
```

Only ``observed`` measurements are fitted. Estimated and missing ones are
counted and kept, never used as outcomes. Negative results stay in. Rows older
than ``max_age_days`` are stale and excluded, and counted.

The fitted model is a set of median measured/predicted ratios with bootstrap
intervals, by stratum: the most specific stratum with enough rows wins, falling
back to coarser ones; with none, it abstains. The model is inactive until
``approve`` records a review reference.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = "agentloop.conditioned_calibration/v1"
DEFAULT_STRATA = (("pass", "task_cluster"), ("pass",))
_FIELDS = {"pass", "task_cluster", "model", "framework", "environment"}
_BASES = {"observed", "estimated", "missing"}


class CalibrationError(ValueError):
    """Observations or settings are malformed."""


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prepare(rows: Sequence[Mapping[str, Any]], as_of: datetime | None, max_age_days: float | None):
    usable, counts = [], {"rows": 0, "not_observed": 0, "stale": 0, "invalid_prediction": 0}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise CalibrationError(f"observation {index} must be a mapping")
        for key in ("pass", "task_cluster"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise CalibrationError(f"observation {index} needs a nonempty {key!r}")
        basis = row.get("measured_basis", "observed")
        if basis not in _BASES:
            raise CalibrationError(f"observation {index} has an unknown measured_basis {basis!r}")
        counts["rows"] += 1
        predicted, measured = _finite(row.get("predicted")), _finite(row.get("measured"))
        if basis != "observed" or measured is None:
            counts["not_observed"] += 1
            continue
        if predicted is None or predicted <= 0:
            counts["invalid_prediction"] += 1
            continue
        if max_age_days is not None:
            seen = _time(row.get("observed_at"))
            if seen is None or as_of is None or as_of - seen > timedelta(days=max_age_days):
                counts["stale"] += 1
                continue
        usable.append({**row, "predicted": predicted, "measured": measured, "_index": index})
    return usable, counts


def _key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in fields)


def _interval(ratios: list[float], seed: int, resamples: int = 1000) -> list[float]:
    rng = random.Random(seed)  # nosec B311 - reproducible statistical resampling
    draws = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(resamples))
    return [
        round(draws[int(0.025 * (resamples - 1))], 6),
        round(draws[int(0.975 * (resamples - 1))], 6),
    ]


def fit(
    rows: Sequence[Mapping[str, Any]],
    *,
    strata: Sequence[Sequence[str]] = DEFAULT_STRATA,
    min_rows: int = 5,
    seed: int = 0,
    as_of: str | None = None,
    max_age_days: float | None = None,
) -> dict[str, Any]:
    """Fit median measured/predicted ratios per stratum. The result is inactive."""
    if type(min_rows) is not int or min_rows < 2:
        raise CalibrationError("min_rows must be an integer of at least 2")
    for level in strata:
        if not level or not set(level) <= _FIELDS:
            raise CalibrationError(f"strata fields must come from {sorted(_FIELDS)}")
    reference = _time(as_of) if as_of is not None else None
    if max_age_days is not None and reference is None:
        raise CalibrationError("max_age_days needs as_of, an ISO timestamp")
    usable, counts = _prepare(rows, reference, max_age_days)
    levels = []
    for fields in strata:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in usable:
            groups.setdefault(_key(row, fields), []).append(row)
        factors = []
        for key, members in sorted(groups.items(), key=lambda item: tuple(map(str, item[0]))):
            ratios = [m["measured"] / m["predicted"] for m in members]
            entry: dict[str, Any] = {
                "stratum": dict(zip(fields, key, strict=True)),
                "rows": len(members),
                "negative_results": sum(1 for m in members if m["measured"] <= 0),
                "with_confounders": sum(1 for m in members if m.get("confounders")),
                "success_rate": _rate([m.get("success") for m in members]),
            }
            if len(members) < min_rows:
                entry["status"] = "insufficient_rows"
            else:
                entry.update(
                    status="fitted",
                    ratio=round(statistics.median(ratios), 6),
                    interval=_interval(ratios, seed),
                )
            factors.append(entry)
        levels.append({"fields": list(fields), "factors": factors})
    return {
        "schema": SCHEMA,
        "levels": levels,
        "min_rows": min_rows,
        "seed": seed,
        "as_of": as_of,
        "max_age_days": max_age_days,
        "counts": counts,
        "active": False,
        "review_ref": None,
    }


def _rate(values: list[Any]) -> float | None:
    known = [v for v in values if isinstance(v, bool)]
    return round(sum(known) / len(known), 6) if known else None


def approve(model: Mapping[str, Any], review_ref: str) -> dict[str, Any]:
    """Return an active copy recording who reviewed it. The input is unchanged."""
    if not isinstance(review_ref, str) or not review_ref.strip():
        raise CalibrationError("approval needs a review reference")
    if model.get("schema") != SCHEMA:
        raise CalibrationError("not a conditioned calibration model")
    return {**model, "active": True, "review_ref": review_ref}


def predict(
    model: Mapping[str, Any],
    context: Mapping[str, Any],
    predicted: float,
    *,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    """Calibrate one prediction, or abstain outside the model's coverage."""
    if not model.get("active") and not allow_unreviewed:
        raise CalibrationError("this calibration has not been reviewed; call approve() first")
    value = _finite(predicted)
    if value is None or value <= 0:
        return {"status": "abstained", "reason": "the prediction must be a positive number"}
    for level in model["levels"]:
        key = {field: context.get(field) for field in level["fields"]}
        match = next((f for f in level["factors"] if f["stratum"] == key), None)
        if match is not None and match["status"] == "fitted":
            low, high = match["interval"]
            return {
                "status": "calibrated",
                "stratum": key,
                "prediction": round(value * match["ratio"], 3),
                "interval": [round(value * low, 3), round(value * high, 3)],
                "rows": match["rows"],
                "reviewed": bool(model.get("active")),
            }
    return {"status": "abstained", "reason": "no fitted stratum covers this context"}


def evaluate(
    rows: Sequence[Mapping[str, Any]],
    *,
    strata: Sequence[Sequence[str]] = DEFAULT_STRATA,
    min_rows: int = 5,
    seed: int = 0,
) -> dict[str, Any]:
    """Leave-one-task-cluster-out comparison of raw and calibrated predictions.

    For each cluster, fit on every other cluster and predict this one. Reports
    mean absolute error, median relative error, interval coverage and
    abstentions for the calibrated predictions, and the same errors for the
    raw (current heuristic) predictions on the same rows.
    """
    usable, counts = _prepare(rows, None, None)
    clusters = sorted({row["task_cluster"] for row in usable})
    if len(clusters) < 2:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "reason": "held-out evaluation needs at least two task clusters",
            "counts": counts,
        }
    raw_errors, cal_errors, raw_rel, cal_rel = [], [], [], []
    covered = abstained = scored = 0
    per_cluster = []
    for cluster in clusters:
        train = [r for r in usable if r["task_cluster"] != cluster]
        test = [r for r in usable if r["task_cluster"] == cluster]
        model = fit(train, strata=strata, min_rows=min_rows, seed=seed)
        cluster_abstained = 0
        for row in test:
            raw_errors.append(abs(row["predicted"] - row["measured"]))
            raw_rel.append(_relative(row["predicted"], row["measured"]))
            result = predict(model, row, row["predicted"], allow_unreviewed=True)
            if result["status"] != "calibrated":
                abstained += 1
                cluster_abstained += 1
                continue
            scored += 1
            cal_errors.append(abs(result["prediction"] - row["measured"]))
            cal_rel.append(_relative(result["prediction"], row["measured"]))
            low, high = result["interval"]
            covered += low <= row["measured"] <= high
        per_cluster.append(
            {"task_cluster": cluster, "rows": len(test), "abstained": cluster_abstained}
        )
    return {
        "schema": SCHEMA,
        "status": "available",
        "split": "leave_one_task_cluster_out",
        "rows": len(usable),
        "counts": counts,
        "raw": {"mae": _mean(raw_errors), "median_relative_error": _median(raw_rel)},
        "calibrated": {
            "mae": _mean(cal_errors),
            "median_relative_error": _median(cal_rel),
            "interval_coverage": round(covered / scored, 6) if scored else None,
            "scored": scored,
            "abstained": abstained,
        },
        "raw_on_scored_rows_mae": _mean(
            [
                abs(row["predicted"] - row["measured"])
                for row in usable
                if _covered_by_other_clusters(row, usable, strata, min_rows)
            ]
        ),
        "success_rate": _rate([row.get("success") for row in usable]),
        "per_cluster": per_cluster,
    }


def _covered_by_other_clusters(row, usable, strata, min_rows) -> bool:
    train = [r for r in usable if r["task_cluster"] != row["task_cluster"]]
    for fields in strata:
        key = _key(row, fields)
        if sum(1 for r in train if _key(r, fields) == key) >= min_rows:
            return True
    return False


def _relative(prediction: float, measured: float) -> float | None:
    return None if measured == 0 else abs(prediction - measured) / abs(measured)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _median(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(statistics.median(known), 6) if known else None


__all__ = ["DEFAULT_STRATA", "SCHEMA", "CalibrationError", "approve", "evaluate", "fit", "predict"]
