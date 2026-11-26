"""Opt-in content fingerprints and the two deterministic rules that read them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop import content_metadata, record_content
from agentloop.content_fingerprints import CONTENT_KEY, MAX_ITEMS, read_content
from agentloop.entrypoint import _quickstart_trace
from agentloop.findings import build_diagnosis
from agentloop.tracer import (
    AgentTrace,
    bind_trace_context,
    record_model_call,
    trace_agent,
    trace_model_call,
    trace_tool_call,
)

CONTENT_RULES = {"unchanged_output", "repeated_result"}


def _content_findings(trace):
    report = trace.report()
    assert report["rule_errors"] == []
    return [item for item in report["finding_candidates"] if item["rule_id"] in CONTENT_RULES]


def _verify(draft, revised, *, name="verify", required=False):
    with trace_model_call(name, model="m", input_tokens=10, output_tokens=2):
        assert record_content(input=draft, output=revised, required=required)


def _find(query, results):
    with trace_tool_call("find"):
        assert record_content(input=query, output=results)


# Capture -----------------------------------------------------------------------


def test_nothing_is_recorded_without_an_active_trace_or_span():
    assert record_content(input="a", output="a") is False
    with trace_agent("no-span") as trace:
        assert record_content(input="a", output="a") is False
    assert trace.events == []


def test_records_attach_to_the_active_span_without_text_or_editing_caller_metadata():
    secret = "Patient 4471 has condition X"
    shared = {"role": "check"}
    with trace_agent("capture") as trace:
        with trace_tool_call("lookup", metadata=shared):
            assert record_content(input=secret, output=[secret, "other"])
    assert shared == {"role": "check"}
    record = trace.events[0].metadata[CONTENT_KEY]
    assert trace.events[0].metadata["role"] == "check"
    assert record["schema"] == "agentloop.content/v1"
    assert len(record["output"]["items"]) == 2
    assert record["output"]["items"][0] == record["input"]["digest"]
    serialized = json.dumps(trace.to_dict())
    assert secret not in serialized and "4471" not in serialized


def test_digests_are_keyed_per_trace_and_survive_a_round_trip():
    digests = []
    for _ in range(2):
        with trace_agent("keyed") as trace:
            _verify("same", "same")
        digests.append(trace.events[0].metadata[CONTENT_KEY])
    assert digests[0]["scope"] != digests[1]["scope"]
    assert digests[0]["input"]["digest"] != digests[1]["input"]["digest"]
    loaded = AgentTrace.from_dict(json.loads(json.dumps(trace.to_dict())))
    assert [item["rule_id"] for item in _content_findings(loaded)] == ["unchanged_output"]


def test_span_id_annotates_an_already_recorded_span_and_content_metadata_merges():
    with trace_agent("explicit") as trace:
        record_model_call(
            "verify",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=5,
            model="m",
            metadata=content_metadata(input="draft", output="draft"),
        )
        record_model_call(
            "verify", started_at="2026-01-01T00:00:01+00:00", duration_ms=5, model="m"
        )
        assert record_content(input="x", output="x", span_id=trace.events[1].event_id)
    records = [read_content(event.metadata) for event in trace.events]
    assert records[0]["scope"] == records[1]["scope"]
    assert records[0]["input"] == records[0]["output"]


def test_normalization_is_conservative():
    with trace_agent("normalize") as trace:
        _verify("answer  \r\nline\n\n", "answer\nline")
        _verify("  def f():\n    return 1", "def f():\n  return 1")
        _verify("Paris", "paris")
        _verify("1", 1)
        _verify({"b": 1, "a": [1, 2]}, {"a": [1, 2], "b": 1})
    same = [
        read_content(event.metadata)["input"]["digest"]
        == read_content(event.metadata)["output"]["digest"]
        for event in trace.events
    ]
    assert same == [True, False, False, False, True]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input": object()},
        {"output": float("nan")},
        {"input": {1: "x"}},
        {},
        {"input": "x", "required": 1},
    ],
)
def test_invalid_values_raise_type_error(kwargs):
    with trace_agent("invalid"), trace_tool_call("step"):
        with pytest.raises(TypeError):
            record_content(**kwargs)


def test_long_lists_keep_only_whole_digest():
    with trace_agent("long") as trace:
        with trace_tool_call("step"):
            record_content(output=list(range(MAX_ITEMS + 1)))
        with trace_tool_call("step"):
            record_content(output=list(range(MAX_ITEMS)))
    first, second = (read_content(event.metadata)["output"] for event in trace.events)
    assert "items" not in first and len(second["items"]) == MAX_ITEMS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(schema="agentloop.content/v2"),
        lambda r: r.update(scope="xyz"),
        lambda r: r["input"].update(digest="not-a-digest"),
        lambda r: r.update(required="no"),
        lambda r: r.update(extra=True),
        lambda r: (r.pop("input"), r.pop("output")),
    ],
)
def test_malformed_records_are_ignored(mutate):
    with trace_agent("malformed") as trace:
        for _ in range(3):
            _verify("draft", "draft")
    for event in trace.events:
        mutate(event.metadata[CONTENT_KEY])
    assert read_content(trace.events[0].metadata) is None
    assert _content_findings(trace) == []


# unchanged_output ------------------------------------------------------------------


def test_existing_traces_without_fingerprints_emit_no_content_findings():
    corpus = json.loads(
        (Path(__file__).parents[1] / "examples/finding_benchmark_corpus.json").read_text(
            encoding="utf-8"
        )
    )
    traces = [
        _quickstart_trace(),
        *(AgentTrace.from_dict(case["trace"]) for case in corpus["cases"]),
    ]
    for trace in traces:
        assert _content_findings(trace) == []


def test_one_unchanged_trailing_check_is_a_low_confidence_finding():
    with trace_agent("one-task") as trace:
        with trace_model_call("draft", model="m", input_tokens=5, output_tokens=5):
            record_content(input="question", output="42")
        _verify("42", "42")
    (finding,) = _content_findings(trace)
    assert finding["type"] == "unchanged_output"
    assert finding["confidence"] == "low"
    assert finding["affected_nodes"] == [trace.events[1].event_id]
    assert finding["observations"]["basis"] == "content_fingerprint"
    assert finding["estimate"]["estimator_id"] == "unchanged_output"
    diagnosis = build_diagnosis(trace)
    (row,) = [item for item in diagnosis["findings"] if item["type"] == "unchanged_output"]
    assert row["rewrite"]["patchable"] is False


def test_confidence_follows_how_often_the_check_was_a_no_op():
    with trace_agent("many") as trace:
        for index in range(5):
            _verify(f"draft {index}", f"draft {index}")
    assert _content_findings(trace)[0]["confidence"] == "medium"

    with trace_agent("mostly") as trace:
        for index in range(5):
            _verify(f"draft {index}", "fixed" if index < 2 else f"draft {index}")
    (finding,) = _content_findings(trace)
    assert finding["confidence"] == "low"
    assert len(finding["affected_nodes"]) == 3
    assert len(finding["observations"]["changed_spans"]) == 2

    with trace_agent("useful") as trace:
        for index in range(5):
            _verify(f"draft {index}", "fixed" if index < 3 else f"draft {index}")
    assert _content_findings(trace) == []


def test_required_failed_and_tool_steps_are_never_flagged():
    with trace_agent("excluded") as trace:
        _verify("a", "a", required=True)
        with trace_tool_call("normalize"):
            record_content(input="a", output="a")
        with pytest.raises(RuntimeError):
            with trace_model_call("verify2", model="m"):
                record_content(input="a", output="a")
                raise RuntimeError("boom")
        _verify("only input", "changed")
    assert _content_findings(trace) == []


def test_records_from_different_scopes_are_not_pooled():
    with trace_agent("scopes") as trace:
        _verify("a", "b")
    reloaded = AgentTrace.from_dict(trace.to_dict())

    with bind_trace_context(reloaded):
        record_model_call(
            "verify",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=1,
            metadata=content_metadata(input="c", output="c"),
        )
        record_model_call(
            "verify",
            started_at="2026-01-01T00:00:01+00:00",
            duration_ms=1,
            metadata=content_metadata(input="d", output="e"),
        )
    scopes = {read_content(event.metadata)["scope"] for event in reloaded.events}
    assert len(scopes) == 2
    # 1 of 2 in the new scope is unchanged (reported); the old changed call is not pooled.
    (finding,) = _content_findings(reloaded)
    assert finding["why"].startswith("1 of 2")


# repeated_result ------------------------------------------------------------------


def _rephrase(question, query):
    with trace_model_call("rephrase", model="m", input_tokens=5, output_tokens=5):
        record_content(input=question, output=query)


def test_rephrased_retrieval_with_nothing_new_flags_the_lookup_and_its_rephrase():
    with trace_agent("rephrase") as trace:
        _find("who wrote it", ["s1", "s2", "s3"])
        _rephrase("who wrote it", "author")
        _find("author", ["s2", "s1"])
        _verify("x", "y", name="answer")
    (finding,) = _content_findings(trace)
    assert finding["type"] == "repeated_result"
    assert finding["affected_nodes"] == [trace.events[1].event_id, trace.events[2].event_id]
    assert finding["observations"]["producer_spans"] == [trace.events[1].event_id]
    assert finding["confidence"] == "low"


def test_new_results_identical_requests_and_shared_producers_are_not_redundant():
    with trace_agent("useful") as trace:
        _find("q", ["s1"])
        _rephrase("q", "query")
        _find("query", ["s1", "s4"])
    assert _content_findings(trace) == []

    with trace_agent("reuse") as trace:
        _find("q", ["s1"])
        _find("q", ["s1"])
    assert _content_findings(trace) == []

    with trace_agent("shared") as trace:
        _find("q", ["s1"])
        _rephrase("q", "query")
        _find("query", ["s1"])
        with trace_model_call("answer", model="m"):
            record_content(input="query", output="final")
    (finding,) = _content_findings(trace)
    assert finding["observations"]["producer_spans"] == []
    assert finding["affected_nodes"] == [trace.events[2].event_id]


def test_results_are_only_compared_within_one_parent_span():
    with trace_agent("batched") as trace:
        for index in range(4):
            with trace_tool_call("task"):
                _find(f"q{index}", ["shared sentence"])
                _find(f"r{index}", ["new"] if index else ["shared sentence"])
    # Pooled across tasks, every later lookup would look redundant. Per task, only
    # one of four rephrased lookups was, which is under half, so nothing is reported.
    assert _content_findings(trace) == []

    with trace_agent("three tasks") as trace:
        for index in range(3):
            with trace_tool_call("task"):
                _find(f"q{index}", [f"s{index}"])
                _rephrase(f"q{index}", f"r{index}")
                _find(f"r{index}", [f"s{index}"])
    (finding,) = _content_findings(trace)
    assert finding["confidence"] == "medium"
    assert len(finding["affected_nodes"]) == 6
