from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from agentloop.costs import estimate_cost_usd


def build_report(trace: Any) -> dict[str, Any]:
    events = trace.events
    model_events = [e for e in events if e.event_type == "model_call"]
    tool_events = [e for e in events if e.event_type == "tool_call"]
    retry_events = [e for e in events if e.event_type == "retry"]

    repeated = repeated_context_stats(model_events)
    parallel = parallelism_opportunities(tool_events)

    return {
        "run_id": trace.run_id,
        "name": trace.name,
        "event_count": len(events),
        "model_call_count": len(model_events),
        "tool_call_count": len(tool_events),
        "retry_count": len(retry_events),
        "total_runtime_ms": round(sum(e.duration_ms for e in events), 3),
        "model_time_ms": round(sum(e.duration_ms for e in model_events), 3),
        "tool_time_ms": round(sum(e.duration_ms for e in tool_events), 3),
        "retry_time_ms": round(sum(e.duration_ms for e in retry_events), 3),
        "input_tokens": sum(e.input_tokens for e in model_events),
        "output_tokens": sum(e.output_tokens for e in model_events),
        "estimated_cost_usd": round(
            sum(estimate_cost_usd(e.model, e.input_tokens, e.output_tokens) for e in model_events),
            6,
        ),
        "repeated_context_tokens": repeated["repeated_context_tokens"],
        "repeated_context_ratio": repeated["repeated_context_ratio"],
        "parallelism_opportunities": parallel,
        "recommendations": build_recommendations(model_events, retry_events, repeated, parallel),
        "events": [e.to_dict() for e in events],
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
