"""Request-local reuse and protected context deduplication (issue #399)."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentloop import request_reuse
from agentloop.request_reuse import (
    REUSE_KEY,
    ReuseError,
    compare_reuse,
    deduplicate_context,
    invalidate,
    request_scope,
    reusable,
    scope_stats,
)
from agentloop.tracer import trace_agent


def counting(effect="read", **options):
    calls = []

    @reusable(tool="catalog.lookup", version="1", effect=effect, **options)
    def lookup(sku, *, detail=False):
        calls.append((sku, detail))
        return {"sku": sku, "tags": ["a"], "detail": detail}

    return lookup, calls


def test_reuse_happens_only_inside_a_request():
    lookup, calls = counting()
    lookup("A")
    lookup("A")
    assert len(calls) == 2  # no scope, no cache
    with request_scope("r1", auth_scope="tenant:a"):
        lookup("A")
        lookup("A")
        lookup("A", detail=True)
        assert scope_stats() == {"hits": 1, "misses": 2, "bypassed": 0, "invalidated": 0}
    assert len(calls) == 4
    assert scope_stats() is None


def test_nothing_crosses_requests_or_tenants():
    lookup, calls = counting()
    with request_scope("r1", auth_scope="tenant:a"):
        lookup("A")
    with request_scope("r2", auth_scope="tenant:a"):
        lookup("A")
    with request_scope("r3", auth_scope="tenant:b"):
        lookup("A")
    assert len(calls) == 3


def test_nested_scopes_are_refused():
    with request_scope("r1", auth_scope="tenant:a"):
        with pytest.raises(ReuseError):
            with request_scope("r2", auth_scope="tenant:b"):
                pass


def test_unsafe_effects_and_model_output_are_refused():
    for effect in ("write", "external", "unknown", None):
        with pytest.raises(ReuseError):
            reusable(tool="t", version="1", effect=effect)
    with pytest.raises(ReuseError, match="allow_model_output"):
        reusable(tool="t", version="1", effect="none", kind="model")
    reusable(tool="t", version="1", effect="none", kind="model", allow_model_output=True)
    with pytest.raises(ReuseError):
        reusable(tool="", version="1", effect="read")


def test_async_and_generator_functions_are_refused():
    decorate = reusable(tool="t", version="1", effect="read")
    with pytest.raises(ReuseError):
        decorate(_async_function)
    with pytest.raises(ReuseError):
        decorate(_generator)


async def _async_function():
    return 1


def _generator():
    yield 1


def test_mutating_a_result_cannot_poison_the_cache():
    lookup, _ = counting()
    with request_scope("r", auth_scope="t"):
        first = lookup("A")
        first["tags"].append("mutated")
        second = lookup("A")
        second["sku"] = "changed"
        third = lookup("A")
    assert third == {"sku": "A", "tags": ["a"], "detail": False}


def test_data_version_and_explicit_invalidation():
    version = {"value": "v1"}
    lookup, calls = counting(data_version=lambda: version["value"])
    with request_scope("r", auth_scope="t"):
        lookup("A")
        lookup("A")
        version["value"] = "v2"
        lookup("A")
        assert len(calls) == 2
        assert invalidate("catalog.lookup") == 2
        lookup("A")
        assert invalidate() == 1
        assert invalidate("other.tool") == 0
    assert len(calls) == 3
    assert invalidate() == 0  # outside a scope


def test_bad_data_version_is_an_error():
    lookup, _ = counting(data_version=lambda: 3)
    with request_scope("r", auth_scope="t"), pytest.raises(ReuseError):
        lookup("A")


@pytest.mark.parametrize(
    "argument", [("a", "b"), {1: "x"}, float("nan"), object(), {"nested": ("tuple",)}]
)
def test_inputs_that_are_not_plain_json_bypass(argument):
    lookup, calls = counting()
    with request_scope("r", auth_scope="t"):
        lookup(argument)
        lookup(argument)
        assert scope_stats()["bypassed"] == 2
    assert len(calls) == 2


def test_hash_collisions_cannot_return_the_wrong_result(monkeypatch):
    lookup, calls = counting()

    class Collide:
        @staticmethod
        def sha256(_data):
            class Digest:
                def hexdigest(self):
                    return "0" * 64

            return Digest()

    monkeypatch.setattr(request_reuse, "hashlib", Collide)
    with request_scope("r", auth_scope="t"):
        assert lookup("A")["sku"] == "A"
        assert lookup("B")["sku"] == "B"  # same digest, different input
    assert len(calls) == 2


def test_errors_are_not_cached():
    attempts = []

    @reusable(tool="flaky", version="1", effect="read")
    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("transient")
        return "ok"

    with request_scope("r", auth_scope="t"):
        with pytest.raises(TimeoutError):
            flaky()
        assert flaky() == "ok"
    assert len(attempts) == 2


def test_uncopyable_results_are_returned_but_not_cached():
    lock = threading.Lock()
    calls = []

    @reusable(tool="locks", version="1", effect="read")
    def make():
        calls.append(1)
        return lock

    with request_scope("r", auth_scope="t"):
        assert make() is lock
        make()
    assert len(calls) == 2


def test_concurrent_requests_are_isolated():
    lookup, calls = counting()
    barrier = threading.Barrier(4)

    def request(index):
        with request_scope(f"r{index}", auth_scope=f"tenant:{index % 2}"):
            barrier.wait()
            lookup("A")
            lookup("A")
            return scope_stats()

    with ThreadPoolExecutor(4) as pool:
        stats = list(pool.map(request, range(4)))
    assert all(item["hits"] == 1 and item["misses"] == 1 for item in stats)
    assert len(calls) == 4


def test_asyncio_tasks_get_their_own_scopes():
    lookup, calls = counting()

    async def request(index):
        with request_scope(f"r{index}", auth_scope="t"):
            lookup("A")
            await asyncio.sleep(0)
            lookup("A")

    async def main():
        await asyncio.gather(*(request(i) for i in range(3)))

    asyncio.run(main())
    assert len(calls) == 3


def test_trace_spans_record_hits_and_reuse_declarations():
    lookup, _ = counting()
    with trace_agent("reuse") as trace, request_scope("r", auth_scope="t"):
        lookup("A")
        lookup("A")
    first, second = trace.events
    assert first.metadata[REUSE_KEY]["hit"] is False
    assert second.metadata[REUSE_KEY]["hit"] is True
    assert first.metadata["tool_reuse_key"] == second.metadata["tool_reuse_key"]
    assert first.metadata["read_only"] is True
    assert first.metadata["agentloop.effects"] == {"effect": "read"}


# Context ------------------------------------------------------------------------


def test_deduplication_keeps_protected_instructions():
    segments = [
        {"id": "sys", "text": "Follow the policy.", "protected": True},
        {"id": "doc1", "text": "Shipping takes 3 days."},
        {"id": "doc2", "text": "Shipping takes 3 days."},
        {"id": "sys2", "text": "Follow the policy.", "protected": True},
        {"id": "echo", "text": "Follow the policy."},
    ]
    result = deduplicate_context(segments)
    assert [s["id"] for s in result["segments"]] == ["sys", "doc1", "sys2"]
    assert result["removed"] == [
        {"id": "doc2", "duplicate_of": "doc1"},
        {"id": "echo", "duplicate_of": "sys"},
    ]
    assert result["protected_kept"] == 2
    assert result["characters_after"] < result["characters_before"]


def test_near_duplicates_are_kept():
    result = deduplicate_context(
        [{"id": "a", "text": "Price: 10"}, {"id": "b", "text": "Price: 10 "}]
    )
    assert result["removed"] == []


@pytest.mark.parametrize(
    "segments",
    [
        [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}],
        [{"text": "x"}],
        [{"id": "a", "text": 3}],
        [{"id": "a", "text": "x", "protected": "yes"}],
        ["not a mapping"],
    ],
)
def test_malformed_segments_are_refused(segments):
    with pytest.raises(ReuseError):
        deduplicate_context(segments)


# Comparison ---------------------------------------------------------------------


def run_lookups(reuse: bool):
    calls = []

    def body(sku):
        calls.append(sku)
        return sku

    tool = reusable(tool="catalog", version="1", effect="read")(body) if reuse else body
    with trace_agent("run") as trace:
        if reuse:
            with request_scope("r", auth_scope="t"):
                for _ in range(3):
                    tool("A")
        else:
            from agentloop.tracer import trace_tool_call

            for _ in range(3):
                with trace_tool_call("catalog"):
                    tool("A")
    return trace, calls


def test_compare_reuse_counts_real_calls():
    baseline, base_calls = run_lookups(False)
    candidate, cand_calls = run_lookups(True)
    assert (len(base_calls), len(cand_calls)) == (3, 1)
    result = compare_reuse(baseline, candidate, baseline_correct=True, candidate_correct=True)
    assert result["candidate"]["executed_tool_calls"] == 1
    assert result["candidate"]["reused_tool_calls"] == 2
    assert result["savings"]["executed_tool_calls"] == 2
    assert result["evidence"] == "measured"


def test_failed_or_unknown_correctness_is_never_an_improvement():
    baseline, _ = run_lookups(False)
    candidate, _ = run_lookups(True)
    failed = compare_reuse(baseline, candidate, baseline_correct=True, candidate_correct=False)
    assert failed["verdict"] == "rejected_incorrect"
    unknown = compare_reuse(baseline, candidate, baseline_correct=True, candidate_correct=None)
    assert unknown["verdict"] == "unverified"
    with pytest.raises(ReuseError):
        compare_reuse(baseline, candidate, baseline_correct=1, candidate_correct=True)


def test_unknown_costs_stay_unknown():
    with trace_agent("costs") as trace:
        from agentloop.tracer import trace_model_call

        with trace_model_call("m", model="x", input_tokens=10, output_tokens=5):
            pass
    summary = compare_reuse(trace, trace, baseline_correct=True, candidate_correct=True)
    assert summary["baseline"]["cost_usd"] is None
    assert summary["savings"]["cost_usd"] is None
    assert summary["baseline"]["tokens"] == 15
