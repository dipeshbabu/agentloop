from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from agentloop.costs import CostEstimate, PricingTable, estimate_cost, load_pricing_table
from agentloop.timing import cumulative_span_time_ms, elapsed_runtime_ms


def build_report(trace: Any) -> dict[str, Any]:
    events = trace.events
    model_events = [e for e in events if e.event_type == "model_call"]
    tool_events = [e for e in events if e.event_type == "tool_call"]
    retry_events = [e for e in events if e.event_type == "retry"]

    repeated = repeated_context_stats(model_events)
    parallel = parallelism_opportunities(tool_events)
    cumulative_time = cumulative_span_time_ms(events)
    cost = cost_breakdown(model_events)

    return {
        "run_id": trace.run_id,
        "name": trace.name,
        "event_count": len(events),
        "model_call_count": len(model_events),
        "tool_call_count": len(tool_events),
        "retry_count": len(retry_events),
        "total_runtime_ms": round(elapsed_runtime_ms(trace), 3),
        "cumulative_span_time_ms": round(cumulative_time, 3),
        "model_time_ms": round(sum(e.duration_ms for e in model_events), 3),
        "tool_time_ms": round(sum(e.duration_ms for e in tool_events), 3),
        "retry_time_ms": round(sum(e.duration_ms for e in retry_events), 3),
        "input_tokens": sum(e.input_tokens for e in model_events),
        "output_tokens": sum(e.output_tokens for e in model_events),
        # Sum of *known* costs only (calculated + provider-reported). A model with
        # no known rate contributes nothing here rather than a fabricated rate;
        # `cost_breakdown` records that some cost was unavailable so this number is
        # never mistaken for a complete measurement. See docs/PRICING.md.
        "estimated_cost_usd": cost["known_cost_usd"],
        "cost_breakdown": cost,
        "repeated_context_tokens": repeated["repeated_context_tokens"],
        "repeated_context_ratio": repeated["repeated_context_ratio"],
        "parallelism_opportunities": parallel,
        "recommendations": build_recommendations(model_events, retry_events, repeated, parallel),
        "events": [e.to_dict() for e in events],
    }


def _event_cost_estimate(event: Any, pricing: PricingTable) -> CostEstimate:
    """Estimate one model call's cost, reading provider metadata from the event.

    Provider, cached-input tokens, and any provider-reported cost ride in
    ``event.metadata`` (a free-form field already in the event schema), so this
    stays compatible with existing serialized traces — a trace without these
    keys simply falls back to model-name resolution.
    """
    metadata = getattr(event, "metadata", None) or {}
    reported = metadata.get("provider_reported_cost_usd", metadata.get("cost_usd"))
    cached = metadata.get("cached_input_tokens", 0)
    try:
        cached_tokens = max(0, int(cached))
    except (TypeError, ValueError):
        cached_tokens = 0
    return estimate_cost(
        event.model,
        event.input_tokens,
        event.output_tokens,
        provider=metadata.get("provider"),
        cached_input_tokens=cached_tokens,
        provider_reported_cost_usd=None if reported is None else float(reported),
        pricing=pricing,
    )


def cost_breakdown(model_events: list[Any], pricing: PricingTable | None = None) -> dict[str, Any]:
    """Aggregate per-call cost estimates, distinguishing the three cost kinds.

    Returns known totals split by source (calculated vs. provider-reported), a
    count and list of model calls whose pricing was unavailable, and the
    provenance (sources and ``as_of`` dates) behind the calculated portion, so a
    report can show exactly how complete and how current its cost number is.
    """
    table = pricing if pricing is not None else load_pricing_table()
    estimates = [_event_cost_estimate(event, table) for event in model_events]

    calculated = round(sum(e.amount_usd or 0.0 for e in estimates if e.state == "calculated"), 6)
    provider_reported = round(
        sum(e.amount_usd or 0.0 for e in estimates if e.state == "provider_reported"), 6
    )
    unknown = [e for e in estimates if e.state == "unknown"]
    sources = sorted(
        {e.pricing_source for e in estimates if e.state == "calculated" and e.pricing_source}
    )
    as_of = sorted(
        {e.pricing_as_of for e in estimates if e.state == "calculated" and e.pricing_as_of}
    )
    unknown_models = sorted({str(e.model) for e in unknown})

    return {
        "known_cost_usd": round(calculated + provider_reported, 6),
        "calculated_usd": calculated,
        "provider_reported_usd": provider_reported,
        "priced_model_call_count": len(estimates) - len(unknown),
        "unavailable_model_call_count": len(unknown),
        "has_unknown_cost": bool(unknown),
        "pricing_sources": sources,
        "pricing_as_of": as_of,
        "unknown_models": unknown_models,
        "model_calls": [e.to_dict() for e in estimates],
    }


def repeated_context_stats(model_events: list[Any]) -> dict[str, Any]:
    prefixes = []
    total_tokens = sum(e.input_tokens for e in model_events)
    for event in model_events:
        words = (event.input_text or "").split()
        if words:
            prefixes.append((" ".join(words[:80]), min(len(words), 80)))
    counts = Counter(prefix for prefix, _ in prefixes)
    repeated_tokens = sum(tokens for prefix, tokens in prefixes if counts[prefix] > 1)
    avoidable = max(0, repeated_tokens // 2)
    ratio = avoidable / total_tokens if total_tokens else 0.0
    return {"repeated_context_tokens": avoidable, "repeated_context_ratio": round(ratio, 4)}


def parallelism_opportunities(tool_events: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in tool_events:
        grouped[event.name].append(event)
    out = []
    for name, items in grouped.items():
        if len(items) >= 3:
            sequential = sum(item.duration_ms for item in items)
            parallel = max(item.duration_ms for item in items)
            out.append(
                {
                    "tool_name": name,
                    "count": len(items),
                    "sequential_time_ms": round(sequential, 3),
                    "estimated_parallel_time_ms": round(parallel, 3),
                    "estimated_savings_ms": round(max(0.0, sequential - parallel), 3),
                }
            )
    return out


def build_recommendations(
    model_events: list[Any],
    retry_events: list[Any],
    repeated: dict[str, Any],
    parallel: list[dict[str, Any]],
) -> list[dict[str, str]]:
    recs = []
    if repeated["repeated_context_ratio"] >= 0.10:
        recs.append(
            {
                "title": "Cache repeated context",
                "description": "Stable instructions appear across multiple model calls. Use cached prefixes or reusable summaries.",
            }
        )
    if parallel:
        recs.append(
            {
                "title": "Parallelize tool calls",
                "description": "Repeated tool calls appear independent. Run them concurrently to lower end-to-end latency.",
            }
        )
    if retry_events:
        recs.append(
            {
                "title": "Use structured outputs",
                "description": "Retries were recorded. Add schema validation or smaller repair prompts.",
            }
        )
    if model_events:
        largest = max(model_events, key=lambda e: e.input_tokens + e.output_tokens)
        if largest.total_tokens >= 4000:
            recs.append(
                {
                    "title": "Compress largest model step",
                    "description": f"The {largest.name} step used {largest.total_tokens} tokens. Consider staged summarization.",
                }
            )
    return recs or [
        {
            "title": "No major pattern detected",
            "description": "Collect more traces for stronger recommendations.",
        }
    ]
