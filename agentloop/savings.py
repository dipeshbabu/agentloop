"""Compatible-selection model for combining per-recommendation savings.

Optimization cards (and the findings derived from them) that touch the same
spans compete for the same work: applying one changes or removes the spans the
other targeted, so their estimates are alternatives, not additive. A realizable
plan therefore applies a pairwise span-disjoint subset of the recommendations.

``select_compatible`` picks that subset: latency savings is the primary
objective and cost savings breaks ties, so both reported totals always come
from the same subset. Small overlap-connected components are solved exactly.
Larger components use a deterministic greedy fallback and the returned
``Selection`` says explicitly that the result is not proven optimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

# Exact maximum-weight independent-set search is exponential in the size of one
# overlap-connected component. Twenty-four keeps the common path exact while
# retaining a deterministic bounded fallback for unusually dense plans.
_EXACT_SEARCH_LIMIT = 24


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
    optimal: bool = True
    algorithm: str = "exact_branch_and_bound"
    exact_component_limit: int = _EXACT_SEARCH_LIMIT


def select_compatible(items: Sequence[SavingsItem]) -> Selection:
    """Return a deterministic compatible subset and its optimality metadata.

    Latency savings is the primary objective and cost savings breaks ties.
    Components up to ``_EXACT_SEARCH_LIMIT`` are solved exactly. Larger
    components use a deterministic greedy fallback; in that case ``optimal`` is
    false so callers cannot accidentally present an approximation as a maximum.
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
    used_approximation = False
    for component in _components(spanned, adjacency):
        if len(component) > _EXACT_SEARCH_LIMIT:
            chosen.extend(_greedy_pick(items, component))
            used_approximation = True
        else:
            chosen.extend(_exact_pick(items, component, adjacency))
    chosen.sort()
    return Selection(
        indices=tuple(chosen),
        latency_ms=sum(items[index].latency_ms for index in chosen),
        cost_usd=sum(items[index].cost_usd for index in chosen),
        optimal=not used_approximation,
        algorithm=("hybrid_exact_greedy" if used_approximation else "exact_branch_and_bound"),
        exact_component_limit=_EXACT_SEARCH_LIMIT,
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
            for neighbor in sorted(adjacency[node], reverse=True):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        yield component


def _savings_key(items: Sequence[SavingsItem], index: int) -> tuple[float, float]:
    return (items[index].latency_ms, items[index].cost_usd)


def _exact_pick(
    items: Sequence[SavingsItem], component: list[int], adjacency: dict[int, frozenset[int]]
) -> list[int]:
    order = sorted(
        component,
        key=lambda index: (-items[index].latency_ms, -items[index].cost_usd, index),
    )
    suffix_latency = [0.0] * (len(order) + 1)
    for position in range(len(order) - 1, -1, -1):
        suffix_latency[position] = suffix_latency[position + 1] + items[order[position]].latency_ms
    best_key = (float("-inf"), float("-inf"))
    best_tuple: tuple[int, ...] | None = None

    def search(
        position: int, blocked: frozenset[int], latency: float, cost: float, picked: list[int]
    ) -> None:
        nonlocal best_key, best_tuple
        if latency + suffix_latency[position] < best_key[0]:
            return
        if position == len(order):
            score = (latency, cost)
            candidate = tuple(sorted(picked))
            if score > best_key or (
                score == best_key and (best_tuple is None or candidate < best_tuple)
            ):
                best_key = score
                best_tuple = candidate
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
    return list(best_tuple or ())


def _greedy_pick(items: Sequence[SavingsItem], component: list[int]) -> list[int]:
    picked: list[int] = []
    used_spans: set[str] = set()
    order = sorted(
        component,
        key=lambda index: (-items[index].latency_ms, -items[index].cost_usd, index),
    )
    for index in order:
        if items[index].spans & used_spans:
            continue
        picked.append(index)
        used_spans |= items[index].spans
    return picked
