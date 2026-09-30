"""Bounded mergeable descriptive statistics; no inferred performance findings."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass


def number(value):
    if type(value) not in {int, float} or value < 0:
        raise ValueError("aggregate values must be finite and nonnegative")
    try:
        if not math.isfinite(value):
            raise ValueError("aggregate values must be finite and nonnegative")
    except OverflowError:
        raise ValueError("aggregate numeric overflow") from None
    return value


def total(left, right):
    return number(left + right)


@dataclass(frozen=True)
class AggregateConfig:
    latency_bounds_ms: tuple[float, ...] = (1, 5, 10, 50, 100, 500, 1000, 5000, 10000, 60000)
    heavy_hitter_capacity: int = 32

    def __post_init__(self):
        if (
            not isinstance(self.latency_bounds_ms, tuple)
            or not 1 <= len(self.latency_bounds_ms) <= 256
        ):
            raise ValueError("latency bounds must be a tuple of 1..256 increasing positive values")
        previous = 0
        for bound in self.latency_bounds_ms:
            if number(bound) <= previous:
                raise ValueError("latency bounds must be strictly increasing and positive")
            previous = bound
        if (
            type(self.heavy_hitter_capacity) is not int
            or not 1 <= self.heavy_hitter_capacity <= 1024
        ):
            raise ValueError("heavy_hitter_capacity must be 1..1024")

    def to_dict(self):
        return {
            "latency_bounds_ms": list(self.latency_bounds_ms),
            "heavy_hitter_capacity": self.heavy_hitter_capacity,
        }


class Histogram:
    def __init__(self, bounds):
        self.bounds = tuple(bounds)
        self.counts = [0] * (len(bounds) + 1)
        self.count = 0
        self.sum = 0.0
        self.minimum = self.maximum = None

    def add(self, value):
        number(value)
        updated = total(self.sum, value)
        self.counts[bisect.bisect_left(self.bounds, value)] += 1
        self.count += 1
        self.sum = updated
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def merge(self, other):
        if self.bounds != other.bounds:
            raise ValueError("incompatible histogram bounds")
        updated = total(self.sum, other.sum)
        self.counts = [a + b for a, b in zip(self.counts, other.counts)]
        self.count += other.count
        self.sum = updated
        if other.count:
            self.minimum = (
                other.minimum if self.minimum is None else min(self.minimum, other.minimum)
            )
            self.maximum = (
                other.maximum if self.maximum is None else max(self.maximum, other.maximum)
            )

    def quantile(self, fraction):
        if not self.count:
            return None
        rank = max(1, math.ceil(self.count * fraction))
        cumulative = 0
        for index, count in enumerate(self.counts):
            cumulative += count
            if cumulative >= rank:
                return {
                    "lower_ms": max(self.minimum, 0 if index == 0 else self.bounds[index - 1]),
                    "upper_ms": min(
                        self.maximum,
                        self.bounds[index] if index < len(self.bounds) else self.maximum,
                    ),
                    "rank": rank,
                }
        raise ValueError("inconsistent histogram counts")

    def to_dict(self):
        return {
            "bounds_ms": list(self.bounds),
            "counts": self.counts[:],
            "count": self.count,
            "sum_ms": self.sum,
            "min_ms": self.minimum,
            "max_ms": self.maximum,
            "mean_ms": self.sum / self.count if self.count else None,
            "quantile_intervals": {str(q): self.quantile(q) for q in (0.5, 0.9, 0.95, 0.99)},
            "algorithm": "fixed_upper_inclusive_bins_v1",
        }


class WeightedCandidates:
    """At most K candidate keys, each with a lower bound and common additive error.

    When full, subtract min(incoming weight, smallest counter) from every counter
    and the incoming weight. The accumulated subtraction bounds any key's loss.
    Merge adds both losses and then feeds surviving counters through the same rule.
    """

    def __init__(self, capacity):
        self.capacity = capacity
        self.weights = {}
        self.error = 0.0
        self.total = 0.0

    def add(self, key, weight):
        number(weight)
        self.total = total(self.total, weight)
        self._add(key, weight)

    def _add(self, key, weight):
        if not weight:
            return
        if key in self.weights:
            self.weights[key] = total(self.weights[key], weight)
        elif len(self.weights) < self.capacity:
            self.weights[key] = weight
        else:
            decrement = min(weight, min(self.weights.values()))
            self.weights = {
                key: value - decrement for key, value in self.weights.items() if value > decrement
            }
            self.error = total(self.error, decrement)
            if weight > decrement:
                self.weights[key] = weight - decrement

    def merge(self, other):
        if self.capacity != other.capacity:
            raise ValueError("incompatible candidate capacities")
        self.total = total(self.total, other.total)
        self.error = total(self.error, other.error)
        for key, weight in sorted(other.weights.items()):
            self._add(key, weight)

    def to_dict(self):
        return {
            "capacity": self.capacity,
            "total_weight": self.total,
            "unlisted_key_upper_bound": self.error,
            "algorithm": "bounded_weighted_decrement_v1",
            "candidates": [
                {
                    "key_sha256": key,
                    "lower_bound": value,
                    "upper_bound": min(self.total, total(value, self.error)),
                }
                for key, value in sorted(self.weights.items(), key=lambda item: (-item[1], item[0]))
            ],
        }
