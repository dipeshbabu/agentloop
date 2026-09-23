from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import (
    Harness,
    HarnessConfig,
    HarnessDeniedError,
    HarnessEscalationError,
    HarnessStoppedError,
)
from agentloop.harness_evidence import (
    HarnessDecisionRecord,
    append_records,
    read_evidence,
    validate_evidence,
)
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.tracer import trace_agent


def make_run(limits=LoopLimits(), *, mode="enforce", budget=None):
    policies = (loop_guard_policy(limits),)
    if budget is not None:
        policies = (budget_policy(budget), *policies)
    return Harness(HarnessConfig(mode, policies)).start_run()


def options(
    arguments=None,
    progress=None,
    *,
    step_id="step",
    retry=None,
    polling=False,
    mutating=False,
    safe=False,
):
    return DispatchOptions(
        retry_source=retry,
        step=StepInfo(
            step_id,
            fingerprint(arguments) if arguments is not None else None,
            fingerprint(progress) if progress is not None else None,
            polling=polling,
            mutating=mutating,
            retry_safe=safe,
        ),
    )


def test_canonical_fingerprints_preserve_values_and_reject_unsupported_inputs():
    assert fingerprint({"b": [2], "a": 1}) == fingerprint({"a": 1, "b": [2]})
    assert fingerprint(1) != fingerprint(True)
    assert fingerprint([1, 2]) != fingerprint([2, 1])
    for value in (float("nan"), {1: "key"}, object(), (1, 2)):
        with pytest.raises(ValueError):
            fingerprint(value)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycles"):
        fingerprint(cycle)


def test_identical_guard_never_dispatches_after_the_configured_bound():
    run = make_run(LoopLimits(max_identical_calls=2))
    calls = []
    target = run.wrap(
        lambda: calls.append(True), boundary="tool", dispatch=options({"query": "x"}, {"done": 0})
    )
    target()
    target()
    with pytest.raises(HarnessStoppedError) as stopped:
        target()
    assert calls == [True, True]
    assert stopped.value.result.proposals[0].decision.reason_code == "identical_step_limit"
    assert "not proof" in stopped.value.result.proposals[0].decision.feedback


@pytest.mark.parametrize("changes", ["arguments", "progress"])
def test_same_name_with_changed_arguments_or_progress_is_not_identical(changes):
    run = make_run(LoopLimits(max_identical_calls=1))

    def target():
        return "ok"

    for index in range(5):
        arguments = {"item": index if changes == "arguments" else 1}
        progress = {"done": index if changes == "progress" else 0}
        assert run.wrap(target, boundary="tool", dispatch=options(arguments, progress))() == "ok"
    assert not run.stopped


def test_oscillation_is_an_explicit_operational_rule_not_a_semantic_claim():
    run = make_run(LoopLimits(oscillation_period=2, oscillation_repeats=3))
    calls = []
    for value in ("a", "b", "a", "b", "a"):
        run.wrap(
            lambda: calls.append(True), boundary="tool", dispatch=options(value, {"done": 0})
        )()
    with pytest.raises(HarnessStoppedError) as stopped:
        run.wrap(lambda: calls.append(True), boundary="tool", dispatch=options("b", {"done": 0}))()
    assert len(calls) == 5
    assert stopped.value.result.proposals[0].decision.reason_code == "oscillation_limit"


def test_declared_polling_is_exempt_but_total_iteration_budget_remains_active():
    run = make_run(
        LoopLimits(max_identical_calls=1, oscillation_period=2),
        budget=BudgetLimits(max_iterations=3),
    )
    target = run.wrap(
        lambda: "poll", boundary="iteration", dispatch=options("same", "same", polling=True)
    )
    for _ in range(3):
        assert target() == "poll"
    with pytest.raises(HarnessDeniedError):
        target()


def test_declared_retries_use_retry_limits_not_no_progress_heuristics():
    run = make_run(LoopLimits(max_retries_per_step=2, max_identical_calls=1))

    def function():
        return "ok"

    run.wrap(function, boundary="tool", dispatch=options("same", "same"))()
    for source in ("framework", "provider"):
        assert (
            run.wrap(function, boundary="tool", dispatch=options("same", "same", retry=source))()
            == "ok"
        )
    with pytest.raises(HarnessStoppedError):
        run.wrap(function, boundary="tool", dispatch=options("same", "same", retry="harness"))()
    before = [result for result in run.results if result.hook.phase == "before"]
    assert before[1].proposals[0].decision.retry_of is not None
    assert "retry_source:framework" in before[1].proposals[0].decision.evidence_refs


@pytest.mark.parametrize("mutating", [None, True])
@pytest.mark.parametrize(
    "failure", [TimeoutError("private timeout"), asyncio.CancelledError("private cancellation")]
)
def test_ambiguous_mutating_retry_escalates_without_repeating_effect(mutating, failure):
    run = make_run()
    effects = []

    def target():
        effects.append(True)
        raise failure

    with pytest.raises(type(failure)) as original:
        run.wrap(target, boundary="tool", dispatch=options("args", "state", mutating=mutating))()
    assert original.value is failure
    with pytest.raises(HarnessEscalationError):
        run.wrap(
            target,
            boundary="tool",
            dispatch=options("args", "state", retry="framework", mutating=mutating),
        )()
    assert effects == [True]


def test_explicit_safe_retry_declaration_permits_idempotent_mutating_work():
    run = make_run(LoopLimits(max_retries_per_step=1))
    assert (
        run.wrap(
            lambda: "deduplicated",
            boundary="tool",
            dispatch=options("same", "same", retry="framework", mutating=True, safe=True),
        )()
        == "deduplicated"
    )


def test_missing_progress_is_a_qualified_hint_without_denial():
    run = make_run(LoopLimits(max_identical_calls=1, oscillation_period=2))
    target = run.wrap(lambda: "ok", boundary="tool", dispatch=options("same", None))
    for _ in range(5):
        assert target() == "ok"
    assert run.results[0].proposals[0].decision.reason_code == "loop_evidence_unavailable"
    assert not run.stopped


def test_shadow_controls_do_not_modify_execution_or_exceptions():
    run = make_run(LoopLimits(max_identical_calls=1), mode="shadow")
    target = run.wrap(lambda: "same", boundary="tool", dispatch=options("same", "same"))
    assert [target() for _ in range(4)] == ["same"] * 4
    assert not run.stopped and all(not result.applied for result in run.results)
    failure = ValueError("private original")

    def fail():
        raise failure

    with pytest.raises(ValueError) as caught:
        run.wrap(fail, boundary="tool", dispatch=options("same", "same"))()
    assert caught.value is failure


def test_branch_and_run_state_are_isolated():
    policy = loop_guard_policy(LoopLimits(max_identical_calls=1))
    harness = Harness(HarnessConfig("enforce", (policy,)))
    for run in (harness.start_run("one"), harness.start_run("two")):
        for branch in ("a", "b"):
            assert (
                run.wrap(
                    lambda: "ok",
                    boundary="tool",
                    branch_id=branch,
                    dispatch=options("same", "same"),
                )()
                == "ok"
            )


def test_concurrent_retries_share_the_step_retry_capacity():
    run = make_run(LoopLimits(max_retries_per_step=1))
    start, entered, release = Barrier(4), Event(), Event()
    calls = []

    def work():
        calls.append(True)
        entered.set()
        assert release.wait(timeout=30)

    def attempt():
        start.wait(timeout=30)
        try:
            run.wrap(work, boundary="tool", dispatch=options("same", "same", retry="framework"))()
            return "done"
        except HarnessStoppedError:
            return "stopped"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(attempt) for _ in range(4)]
        assert entered.wait(timeout=30)
        release.set()
        outcomes = [future.result(timeout=30) for future in futures]
    assert calls == [True] and outcomes.count("done") == 1


def test_budget_denial_does_not_consume_the_step_retry_allowance():
    run = make_run(LoopLimits(max_retries_per_step=1), budget=BudgetLimits(max_model_calls=0))
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("model dispatched"),
            boundary="model",
            dispatch=options("same", "same", retry="framework"),
        )()
    assert (
        run.wrap(
            lambda: "tool", boundary="tool", dispatch=options("same", "same", retry="framework")
        )()
        == "tool"
    )


def test_bounded_feedback_and_fingerprints_round_trip_without_input_payloads():
    secret = "private prompt and state"
    with trace_agent("loop-evidence") as trace:
        run = make_run(LoopLimits(max_identical_calls=1), mode="shadow")
        target = run.wrap(lambda: None, boundary="tool", dispatch=options(secret, secret))
        target()
        target()
    artifact = read_evidence(trace)
    assert artifact["schema_version"] == "1.1"
    assert secret not in json.dumps(artifact)
    assert all(
        record["feedback"] is None or len(record["feedback"]) <= 256
        for record in artifact["decisions"].values()
    )
    assert read_evidence(trace_from_otel(trace_to_otel(trace))) == artifact


def test_legacy_decision_records_keep_their_identity_and_payload_when_upgraded():
    with trace_agent("compatibility") as trace:
        run = make_run(mode="shadow")
        run.wrap(lambda: None, boundary="tool", dispatch=options("x", "p"))()
    legacy = copy.deepcopy(read_evidence(trace))
    legacy["schema_version"] = "1.0"
    for record in legacy["decisions"].values():
        record["schema_version"] = "1.0"
        record.pop("feedback")
    preserved = copy.deepcopy(legacy)
    assert validate_evidence(legacy) == preserved
    for record in legacy["decisions"].values():
        assert HarnessDecisionRecord.from_dict(record).to_dict() == record
    # New invocations upgrade the envelope without rewriting old record versions.
    with trace_agent("new") as other:
        extra = make_run(mode="shadow")
        extra.wrap(lambda: None, boundary="tool", dispatch=options("x", "p"))()
    new = read_evidence(other)
    append_records(
        legacy,
        tuple(HarnessDecisionRecord.from_dict(record) for record in new["decisions"].values()),
        new["policies"],
    )
    assert legacy["schema_version"] == "1.1"
    for identity, record in preserved["decisions"].items():
        assert legacy["decisions"][identity] == record


def test_unmarked_identical_mutation_after_timeout_does_not_repeat_the_effect():
    run = make_run()
    effects = []

    def target():
        effects.append(True)
        raise TimeoutError("ambiguous")

    dispatch = options({"record": 1}, {"done": 0}, mutating=True)
    with pytest.raises(TimeoutError):
        run.wrap(target, boundary="tool", dispatch=dispatch)()
    with pytest.raises(HarnessEscalationError):
        run.wrap(target, boundary="tool", dispatch=dispatch)()
    assert effects == [True]


def test_overlap_does_not_manufacture_a_sequential_no_progress_cycle():
    async def exercise():
        run = make_run(LoopLimits(max_identical_calls=1, oscillation_period=2))
        entered, release = asyncio.Event(), asyncio.Event()

        async def first():
            entered.set()
            await release.wait()

        dispatch = options("same", "same")
        pending = asyncio.create_task(run.wrap(first, boundary="tool", dispatch=dispatch)())
        await entered.wait()
        assert run.wrap(lambda: "parallel", boundary="tool", dispatch=dispatch)() == "parallel"
        release.set()
        await pending
        assert not run.stopped
        assert any(
            result.proposals[0].decision.reason_code == "loop_concurrent_evidence"
            for result in run.results
        )
        assert run.wrap(lambda: "fresh", boundary="tool", dispatch=dispatch)() == "fresh"

    asyncio.run(exercise())


def test_denied_overlap_does_not_erase_history_of_real_work():
    from agentloop.harness import Decision, Hook, Policy

    def gate(context):
        context.state["calls"] = context.state.get("calls", 0) + 1
        return Decision("deny" if context.state["calls"] == 2 else "continue")

    denial = Policy("gate", "1", gate, hooks={Hook("tool")}, actions={"continue", "deny"})
    run = Harness(
        HarnessConfig("enforce", (denial, loop_guard_policy(LoopLimits(max_identical_calls=1))))
    ).start_run()
    dispatch = options("same", "same")

    def stream():
        yield "work"

    active = run.wrap(stream, boundary="tool", dispatch=dispatch)()
    assert next(active) == "work"
    with pytest.raises(HarnessDeniedError):
        run.wrap(
            lambda: pytest.fail("denied overlap dispatched"), boundary="tool", dispatch=dispatch
        )()
    with pytest.raises(StopIteration):
        next(active)
    with pytest.raises(HarnessStoppedError):
        run.wrap(lambda: pytest.fail("real history was lost"), boundary="tool", dispatch=dispatch)()


def test_declared_polling_breaks_a_semantic_repetition_sequence():
    run = make_run(LoopLimits(max_identical_calls=1))
    run.wrap(lambda: None, boundary="tool", dispatch=options("same", "same"))()
    run.wrap(lambda: None, boundary="tool", dispatch=options("same", "same", polling=True))()
    assert run.wrap(lambda: "new", boundary="tool", dispatch=options("same", "same"))() == "new"


def test_missing_step_identity_on_a_retry_escalates_before_dispatch():
    run = make_run()
    with pytest.raises(HarnessEscalationError):
        run.wrap(
            lambda: pytest.fail("unknown retry dispatched"),
            boundary="tool",
            dispatch=DispatchOptions(retry_source="framework"),
        )()


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_retry_limits_are_rejected(value):
    with pytest.raises(ValueError):
        LoopLimits(max_retries_per_step=value)


def test_feedback_is_bounded_and_escaped_in_html():
    from agentloop.entrypoint import _analysis_payload
    from agentloop.harness import Decision, Policy
    from agentloop.html_report import analysis_to_html

    with pytest.raises(ValueError, match="256"):
        Decision(feedback="x" * 257)
    with trace_agent("feedback") as trace:
        policy = Policy(
            "feedback", "1", lambda context: Decision(feedback='<script>alert("x")</script>')
        )
        run = Harness(HarnessConfig("shadow", (policy,))).start_run()
        run.wrap(lambda: None, boundary="model")()
    html = analysis_to_html(_analysis_payload(trace))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_feedback_schema_upgrade_does_not_change_the_comparison_link_contract():
    from agentloop.harness_evidence import comparison_evidence

    with trace_agent("before") as baseline:
        pass
    with trace_agent("after") as candidate:
        run = make_run(mode="shadow")
        run.wrap(lambda: None, boundary="tool", dispatch=options("x", "p"))()
    linked = comparison_evidence(baseline, candidate)
    assert linked["schema_version"] == "1.0"
    assert linked["candidate"]["schema_version"] == "1.1"


def test_new_records_cannot_be_mislabeled_as_a_legacy_envelope():
    from agentloop.harness_evidence import HarnessEvidenceError

    with trace_agent("mislabeled") as trace:
        run = make_run(mode="shadow")
        run.wrap(lambda: None, boundary="tool", dispatch=options("x", "p"))()
    artifact = read_evidence(trace)
    artifact["schema_version"] = "1.0"
    with pytest.raises(HarnessEvidenceError, match="envelope"):
        validate_evidence(artifact)
