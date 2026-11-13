from __future__ import annotations

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"retry_reason": "transport_timeout"},
        {"retry_reason": "rate_limit"},
        {"retry_reason": []},
        {"retry_reason": "unknown"},
        {"retry_reason": "transport_timeout", "reason": "schema_validation_failed"},
    ],
)
def test_unrelated_or_unknown_retry_causes_do_not_recommend_schema_changes(metadata):
    trace = retry_trace(metadata)
    report = trace.report()
    assert report["retry_count"] == 1
    for cards in (
        report["finding_candidates"],
        build_optimization_plan(trace, report)["optimization_cards"],
        build_diagnosis(trace)["findings"],
    ):
        assert not any(card["type"] == "add_schema_validation" for card in cards)


def retry_trace(metadata):
    trace = AgentTrace("retry-cause", elapsed_ms=100)
    trace.add_event(
        AgentEvent(
            event_id="retry",
            run_id=trace.run_id,
            event_type="retry",
            name="repair_json",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:00.100+00:00",
            duration_ms=100,
            metadata=metadata,
        )
    )
    return trace


@pytest.mark.parametrize(
    "metadata",
    [
        {"retry_reason": "invalid_output_schema"},
        {"reason": "schema_validation_failed"},
    ],
)
def test_schema_recommendations_keep_their_cause_and_estimate_scope(metadata):
    trace = retry_trace(metadata)
    unrelated = retry_trace({"retry_reason": "transport_timeout"}).events[0]
    unrelated.run_id = trace.run_id
    unrelated.event_id = "transport"
    unrelated.duration_ms = 900
    unrelated.started_at = "2026-01-01T00:00:00.100+00:00"
    unrelated.ended_at = "2026-01-01T00:00:01+00:00"
    trace.elapsed_ms = 1000
    trace.add_event(unrelated)
    cards = [
        card
        for card in trace.report()["finding_candidates"]
        if card["type"] == "add_schema_validation"
    ]
    assert len(cards) == 1
    assert cards[0]["affected_nodes"] == ["retry"]
    assert cards[0]["estimated_latency_savings_ms"] == 80
    assert cards[0]["rule_version"] == "1.2"
