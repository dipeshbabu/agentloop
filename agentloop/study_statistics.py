"""Shared task-weighted descriptive statistics for offline evidence artifacts."""

from __future__ import annotations

import random
import statistics
from collections import defaultdict

from agentloop.studies import summarize_values


def _percentile(values, fraction):
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def task_weighted_summary(observations, settings):
    """Summarize validated (task ID, finite value or None) observations.

    Missing observations remain in the planned denominator. Available values are
    averaged within each task; bootstrap draws then give each observed task equal
    weight. This does not remedy missingness or selection bias.
    """
    values, grouped, planned_tasks = [], defaultdict(list), set()
    for task, value in observations:
        planned_tasks.add(task)
        values.append(value)
        if value is not None:
            grouped[task].append(value)
    means = [statistics.fmean(grouped[key]) for key in sorted(grouped)]
    interval = {
        "method": "percentile_bootstrap_equal_weight_task_means",
        **settings,
        "task_count": len(means),
        "lower": None,
        "upper": None,
        "status": "insufficient_tasks",
    }
    if len(means) >= 2:
        rng = random.Random(settings["seed"])  # nosec B311 - statistical resampling
        samples = sorted(
            statistics.fmean(rng.choices(means, k=len(means))) for _ in range(settings["samples"])
        )
        tail = (1 - settings["confidence"]) / 2
        interval.update(
            lower=_percentile(samples, tail),
            upper=_percentile(samples, 1 - tail),
            status="computed",
        )
    return {
        "observation_summary": summarize_values(values),
        "task_summary": summarize_values(means),
        "interval": interval,
        "planned_observation_count": len(values),
        "planned_task_count": len(planned_tasks),
    }
