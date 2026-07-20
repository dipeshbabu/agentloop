from __future__ import annotations

import pytest

from agentloop.schema import SCHEMA_VERSION, TraceValidationError
from agentloop.tracer import AgentTrace, trace_agent, trace_model_call


def _valid_04_trace() -> dict:
    """A serialized trace as 0.4 wrote it: no schema_version field."""

    return {
        "name": "legacy",
        "run_id": "run_legacy",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "elapsed_ms": 1000.0,
        "metadata": {},
        "events": [
            {
                "event_id": "evt_1",
                "run_id": "run_legacy",
                "event_type": "model_call",
                "name": "plan",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 1000.0,
                "input_tokens": 10,
                "output_tokens": 5,
                "status": "ok",
                "metadata": {},
            }
        ],
    }


def test_to_dict_includes_schema_version() -> None:
    with trace_agent("with-version") as trace:
        with trace_model_call("call"):
            pass
    assert trace.to_dict()["schema_version"] == SCHEMA_VERSION


def test_legacy_04_trace_without_schema_version_still_loads() -> None:
    trace = AgentTrace.from_dict(_valid_04_trace())
    assert trace.run_id == "run_legacy"
    assert trace.events[0].input_tokens == 10


def test_unknown_fields_are_ignored_not_rejected() -> None:
    data = _valid_04_trace()
    data["future_top_level"] = {"anything": 1}
    data["events"][0]["future_event_field"] = "surprise"

    trace = AgentTrace.from_dict(data)

    assert trace.events[0].name == "plan"
    assert not hasattr(trace.events[0], "future_event_field")


def test_round_trip_is_stable() -> None:
    with trace_agent("stable") as trace:
        with trace_model_call("call", input_tokens=3, output_tokens=2):
            pass
    reloaded = AgentTrace.from_dict(trace.to_dict())
    assert reloaded.to_dict() == trace.to_dict()


def test_missing_required_event_field_raises_with_location() -> None:
    data = _valid_04_trace()
    del data["events"][0]["event_type"]

    with pytest.raises(TraceValidationError) as excinfo:
        AgentTrace.from_dict(data)

    assert excinfo.value.field == "events[0].event_type"


def test_duplicate_event_ids_are_rejected() -> None:
    data = _valid_04_trace()
    data["events"].append(dict(data["events"][0]))

    with pytest.raises(TraceValidationError) as excinfo:
        AgentTrace.from_dict(data)

    assert excinfo.value.field == "events[1].event_id"


def test_unsupported_status_is_rejected() -> None:
    data = _valid_04_trace()
    data["events"][0]["status"] = "weird"

    with pytest.raises(TraceValidationError) as excinfo:
        AgentTrace.from_dict(data)

    assert excinfo.value.field == "events[0].status"
