"""Compatible-selection model for combining per-recommendation savings.

Optimization cards (and the findings derived from them) that touch the same
spans compete for the same work: applying one changes or removes the spans the
other targeted, so their estimates are alternatives, not additive. A realizable
plan therefore applies a pairwise span-disjoint subset of the recommendations.

``select_compatible`` picks that subset: latency savings is the primary
objective and cost savings breaks ties, so both reported totals always come
from the same subset — the (latency, cost) pair is achievable by one concrete
plan rather than a mix of incompatible alternatives. Items without affected
spans do not compete with anything and always count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

# Beyond this many mutually overlapping items the exact search (exponential in
# the size of the overlap-connected component) falls back to a greedy pick.
_EXACT_SEARCH_LIMIT = 16


@dataclass(frozen=True)
class SavingsItem:
    spans: frozenset[str]
    latency_ms: float = 0.0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Selection:
    indices: tuple[int, ...]
    latency_ms: float
    cost_usd: float


def select_compatible(items: Sequence[SavingsItem]) -> Selection:
    """Return the compatible (span-disjoint) subset with the best savings.

    Maximizes total latency savings, breaking ties by total cost savings.
    Span-free items are always selected.
    """
    chosen = [index for index, item in enumerate(items) if not item.spans]
    spanned = [index for index, item in enumerate(items) if item.spans]
    neighbor_sets: dict[int, set[int]] = {index: set() for index in spanned}
    for position, left in enumerate(spanned):
        for right in spanned[position + 1 :]:
            if items[left].spans & items[right].spans:
                neighbor_sets[left].add(right)
                neighbor_sets[right].add(left)
    adjacency = {index: frozenset(neighbors) for index, neighbors in neighbor_sets.items()}
    for component in _components(spanned, adjacency):
        if len(component) > _EXACT_SEARCH_LIMIT:
            chosen.extend(_greedy_pick(items, component))
        else:
            chosen.extend(_exact_pick(items, component, adjacency))
    chosen.sort()
    return Selection(
        indices=tuple(chosen),
        latency_ms=sum(items[index].latency_ms for index in chosen),
        cost_usd=sum(items[index].cost_usd for index in chosen),
    )


def _components(nodes: list[int], adjacency: dict[int, frozenset[int]]) -> Iterator[list[int]]:
    seen: set[int] = set()
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        component: list[int] = []
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        yield component


def _savings_key(items: Sequence[SavingsItem], index: int) -> tuple[float, float]:
    return (items[index].latency_ms, items[index].cost_usd)


def _exact_pick(
    items: Sequence[SavingsItem], component: list[int], adjacency: dict[int, frozenset[int]]
) -> list[int]:
    order = sorted(component, key=lambda index: _savings_key(items, index), reverse=True)
    suffix_latency = [0.0] * (len(order) + 1)
    for position in range(len(order) - 1, -1, -1):
        suffix_latency[position] = suffix_latency[position + 1] + items[order[position]].latency_ms
    best_key = (float("-inf"), float("-inf"))
    best: list[int] = []

    def search(
        position: int, blocked: frozenset[int], latency: float, cost: float, picked: list[int]
    ) -> None:
        nonlocal best_key, best
        if latency + suffix_latency[position] < best_key[0]:
            return
        if position == len(order):
            if (latency, cost) > best_key:
                best_key = (latency, cost)
                best = list(picked)
            return
        index = order[position]
        if index not in blocked:
            picked.append(index)
            search(
                position + 1,
                blocked | adjacency[index],
                latency + items[index].latency_ms,
                cost + items[index].cost_usd,
                picked,
            )
            picked.pop()
        search(position + 1, blocked, latency, cost, picked)

    search(0, frozenset(), 0.0, 0.0, [])
    return best


def _greedy_pick(items: Sequence[SavingsItem], component: list[int]) -> list[int]:
    picked: list[int] = []
    used_spans: set[str] = set()
    for index in sorted(component, key=lambda index: _savings_key(items, index), reverse=True):
        if items[index].spans & used_spans:
            continue
        picked.append(index)
        used_spans |= items[index].spans
    return picked
