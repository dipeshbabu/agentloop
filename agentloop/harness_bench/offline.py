"""Deterministic offline behavior probes, counters and source-scoped drift."""

from __future__ import annotations

import asyncio
import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from agentloop.harness import (
    ACTIONS,
    Decision,
    HarnessConfig,
    HarnessControlError,
    HarnessDeniedError,
    Hook,
    Policy,
)
from agentloop.harness_bench.drivers import OfflineDriver
from agentloop.interoperability.artifacts import write_artifact
from agentloop.interoperability.registry import (
    AdapterManifest,
    CapabilityObservation,
    _probe_observation,
    capability_report,
    get_adapter,
)
from agentloop.interoperability.validation import ImportValidationError
from agentloop.interventions import canonical_json
from agentloop.tracer import trace_agent, trace_tool_call

PROBE_VERSION = "1.0"
_PROBES = (
    ("basic-sync", "lifecycle.sync"),
    ("basic-async", "lifecycle.async"),
    ("tool-observation", "observation.tool_calls"),
    ("pretool-deny", "enforcement.pre_tool"),
    ("premodel-deny", "enforcement.pre_model"),
    ("sync-stream", "lifecycle.stream"),
    ("async-stream", "lifecycle.async_stream"),
    ("partial-close", "lifecycle.partial_close"),
    ("cancel", "lifecycle.cancel"),
    ("policy-failure", "enforcement.policy_failure"),
    ("repeated-wrap", "observation.outcomes"),
    ("model-override", "compatibility.model_override"),
    ("approval", "compatibility.approval"),
    ("resume", "lifecycle.resume"),
    ("fork", "lifecycle.fork"),
)


class FakeClock:
    def __init__(self):
        self.tick = 0

    def ns(self):
        self.tick += 1_000_000
        return self.tick

    def utc(self):
        return "2000-01-01T00:00:00+00:00"


def _config(action="continue", boundary="tool", *, broken=False):
    def policy(_context):
        if broken:
            raise ValueError("synthetic policy failure")
        return Decision(action, reason_code="offline_probe")

    return HarnessConfig(
        "enforce",
        (
            Policy(
                "offline-probe",
                PROBE_VERSION,
                policy,
                hooks=frozenset({Hook(boundary)}),
                actions=ACTIONS,
            ),
        ),
    )


def _hooks(run) -> list[dict]:
    if run is None:
        return []
    return [
        {
            "boundary": item.hook.boundary,
            "phase": item.hook.phase,
            "action": item.action,
            "applied": item.applied,
            "dispatched": item.dispatched,
            "status": item.status,
            "policy_failed": any(value.failed for value in item.proposals),
        }
        for item in run.results
    ]


def _errors_preserved(driver: OfflineDriver) -> bool:
    """Check original failure identity at each supported callable lifecycle."""
    for kind in ("sync", "async", "generator", "async_generator"):
        original = ValueError("synthetic invocation failure")

        def fail():
            raise original

        async def afail():
            raise original

        def stream():
            raise original
            yield

        async def astream():
            raise original
            yield

        wrapped, _ = driver.bind(
            _config(),
            {"sync": fail, "async": afail, "generator": stream, "async_generator": astream}[kind],
            kind,
        )
        try:
            if kind == "sync":
                wrapped()
            elif kind == "generator":
                next(wrapped())
            else:

                async def consume():
                    return await wrapped() if kind == "async" else await wrapped().__anext__()

                asyncio.run(consume())
        except ValueError as exc:
            if exc is not original:
                return False
        else:
            return False
    return True


def _probe(driver: OfflineDriver, probe_id: str) -> dict:
    calls = []
    if probe_id in {"basic-sync", "basic-async", "repeated-wrap", "tool-observation"}:
        expected = {"typed_output": 7}

        def work(value, *, marker):
            calls.append((value, marker))
            run = get_run()
            call_id = run.results[-1].call_id if run is not None and run.results else None
            with trace_tool_call(
                "offline-probe-tool", metadata={"source_harness_call_id": call_id}
            ):
                return expected

        async def awork(value, *, marker):
            return work(value, marker=marker)

        kind = "async" if probe_id == "basic-async" else "sync"
        wrapped, get_run = driver.bind(
            _config(),
            awork if kind == "async" else work,
            kind,
            repeated=probe_id == "repeated-wrap",
        )
        with trace_agent("offline-capability-probe") as trace:
            actual = (
                asyncio.run(wrapped(3, marker="safe"))
                if kind == "async"
                else wrapped(3, marker="safe")
            )
        hooks = _hooks(get_run())
        observed = [event for event in trace.events if event.operation_kind == "tool"]
        call_ids = (
            {item.call_id for item in get_run().results if item.dispatched}
            if get_run() is not None
            else set()
        )
        correlated = bool(observed) and all(
            event.metadata.get("source_harness_call_id") in call_ids for event in observed
        )
        errors_preserved = _errors_preserved(driver) if probe_id == "repeated-wrap" else True
        return {
            "passed": actual is expected
            and calls == [(3, "safe")]
            and correlated
            and any(item["dispatched"] for item in hooks)
            and errors_preserved,
            "protected_callable_dispatches": len(calls),
            "return_identity_preserved": actual is expected,
            "tool_spans": len(observed),
            "tool_identity_correlated": correlated,
            "original_errors_preserved": errors_preserved,
            "hook_records": hooks,
        }
    if probe_id in {"pretool-deny", "premodel-deny", "policy-failure"}:
        boundary = "model" if probe_id == "premodel-deny" else "tool"

        def work(value):
            calls.append(value)
            return 7

        async def awork(value):
            return work(value)

        def stream(value):
            yield work(value)

        async def astream(value):
            yield work(value)

        acknowledgements, hooks = [], []
        for kind in ("sync", "async", "generator", "async_generator"):
            wrapped, get_run = driver.bind(
                _config("deny", boundary, broken=probe_id == "policy-failure"),
                {"sync": work, "async": awork, "generator": stream, "async_generator": astream}[
                    kind
                ],
                kind,
                boundary=boundary,
            )
            denied = False
            try:
                if kind == "sync":
                    wrapped(3)
                elif kind == "generator":
                    next(wrapped(3))
                else:

                    async def consume():
                        return await wrapped(3) if kind == "async" else await wrapped(3).__anext__()

                    asyncio.run(consume())
            except HarnessControlError as exc:
                denied = isinstance(exc, HarnessDeniedError) or probe_id == "policy-failure"
            acknowledgements.append(denied)
            hooks.extend(_hooks(get_run()))
        denied = all(acknowledgements)
        policy_failed = any(item["policy_failed"] for item in hooks)
        return {
            "passed": denied
            and not calls
            and any(
                item["action"]
                in ({"deny", "stop", "escalate"} if probe_id == "policy-failure" else {"deny"})
                and item["applied"]
                and not item["dispatched"]
                for item in hooks
            )
            and (probe_id != "policy-failure" or policy_failed),
            "protected_callable_dispatches": len(calls),
            "denial_acknowledged": denied,
            "tested_lifecycles": ["sync", "async", "generator", "async_generator"],
            "policy_failure_recorded": policy_failed,
            "hook_records": hooks,
        }
    if probe_id in {"sync-stream", "async-stream", "partial-close"}:
        state = {"entered": False, "finished": False, "closed": False, "chunks": 0}

        def stream():
            state["entered"] = True
            try:
                state["chunks"] += 1
                yield 1
                state["chunks"] += 1
                yield 2
                state["finished"] = True
            finally:
                state["closed"] = True

        async def astream():
            for value in stream():
                yield value

        kind = "async_generator" if probe_id == "async-stream" else "generator"
        wrapped, get_run = driver.bind(
            _config(), astream if kind == "async_generator" else stream, kind
        )

        async def consume_async():
            source = wrapped()
            lazy = not state["entered"]
            first = await source.__anext__()
            before_finish = not state["finished"] and state["chunks"] == 1
            second = await source.__anext__()
            try:
                await source.__anext__()
            except StopAsyncIteration:
                pass
            await source.aclose()
            return lazy, first, before_finish, second

        if kind == "async_generator":
            lazy, first, before_finish, second = asyncio.run(consume_async())
        else:
            source = wrapped()
            lazy = not state["entered"]
            first = next(source)
            before_finish = not state["finished"] and state["chunks"] == 1
            if probe_id == "partial-close":
                source.close()
                second = None
            else:
                second = next(source)
                try:
                    next(source)
                except StopIteration:
                    pass
                source.close()
        passed = (
            lazy
            and first == 1
            and before_finish
            and state["closed"]
            and (
                state["chunks"] == 1 and not state["finished"]
                if probe_id == "partial-close"
                else second == 2 and state["finished"]
            )
        )
        return {
            "passed": passed,
            "lazy_entry": lazy,
            "first_chunk_before_completion": before_finish,
            **state,
            "hook_records": _hooks(get_run()),
        }
    if probe_id == "cancel":
        state = {"entered": False, "cancellation_acknowledged": False, "finally_executed": False}

        async def scenario():
            started, blocked = asyncio.Event(), asyncio.Event()

            async def work():
                state["entered"] = True
                started.set()
                try:
                    await blocked.wait()
                finally:
                    state["finally_executed"] = True

            wrapped, get_run = driver.bind(_config(), work, "async")
            task = asyncio.create_task(wrapped())
            starter = asyncio.create_task(started.wait())
            done, _ = await asyncio.wait({task, starter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done and not started.is_set():
                starter.cancel()
                await asyncio.gather(starter, return_exceptions=True)
                return []
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                state["cancellation_acknowledged"] = True
            await starter
            return _hooks(get_run())

        hooks = asyncio.run(scenario())
        return {
            "passed": all(state.values()) and any(item["status"] == "cancelled" for item in hooks),
            **state,
            "hook_records": hooks,
        }
    raise ValueError("unsupported offline probe")


@dataclass(frozen=True)
class BenchResult:
    observations: tuple[CapabilityObservation, ...]
    _report_json: str
    _proofs_json: str

    def report(self) -> dict:
        return json.loads(self._report_json)

    def write(self, out: str | Path) -> Path:
        root = Path(out)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        for reference, proof in json.loads(self._proofs_json).items():
            write_artifact(root, reference, (canonical_json(proof) + "\n").encode())
        write_artifact(root, "capability-report.json", (self._report_json + "\n").encode())
        rows = self.report()["capabilities"]
        markdown = (
            "# Offline capability bench\n\nExplicit instrumented boundaries only; hidden SDK work and vendor behavior remain unverified.\n\n| Capability | State |\n| --- | --- |\n"
            + "".join(f"| {row['name']} | {row['state']} |\n" for row in rows)
        )
        write_artifact(root, "capability-report.md", markdown.encode())
        return root / "capability-report.json"


def run_offline_bench(
    adapter_id="python-wrapped",
    *,
    driver: OfflineDriver | None = None,
    manifest: AdapterManifest | None = None,
    test_timestamp: str | None = None,
) -> BenchResult:
    """Execute only trusted local fakes; optional SDK absence yields real skips."""
    manifest = manifest or get_adapter(adapter_id)
    driver = driver or OfflineDriver(adapter_id)
    if driver.adapter_id != manifest.adapter_id:
        raise ImportValidationError(
            "invalid_probe_driver", "adapter", "driver identity differs from its manifest"
        )
    stamp = test_timestamp or datetime.now(timezone.utc).isoformat()
    environment = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "adapter_code_fingerprint": manifest.code_fingerprint,
        "upstream_version": manifest.upstream_version,
    }
    env_hash = sha256(canonical_json(environment).encode()).hexdigest()
    observations, proofs = [], {}
    unavailable = driver.unavailable_reason()
    for probe_id, capability in _PROBES:
        declaration = next(item for item in manifest.capabilities if item.name == capability)
        reason = unavailable or ("live_probe_required" if declaration.requires_live else None)
        reference = f"artifacts/{probe_id}.json"
        if reason:
            proof = {
                "schema_version": "1.0",
                "probe_id": probe_id,
                "probe_version": PROBE_VERSION,
                "status": "skipped",
                "reason": reason,
            }
            verdict = "skipped"
        else:
            clock = FakeClock()
            try:
                with (
                    patch("agentloop.harness.perf_counter_ns", clock.ns),
                    patch("agentloop.harness.utc_now_iso", clock.utc),
                ):
                    values = _probe(driver, probe_id)
                verdict = "verified_supported" if values["passed"] else "verified_failed"
                proof = {
                    "schema_version": "1.0",
                    "probe_id": probe_id,
                    "probe_version": PROBE_VERSION,
                    "status": verdict,
                    "clock_source": "synthetic_monotonic_clock",
                    **values,
                }
            except Exception as exc:
                verdict, reason = "verified_failed", "driver_or_contract_failure"
                proof = {
                    "schema_version": "1.0",
                    "probe_id": probe_id,
                    "probe_version": PROBE_VERSION,
                    "status": verdict,
                    "error_type": type(exc).__name__,
                }
        proofs[reference] = proof
        observations.append(
            _probe_observation(
                capability=capability,
                verdict=verdict,
                probe_id=probe_id,
                probe_version=PROBE_VERSION,
                tested_adapter_version=manifest.adapter_version,
                tested_transport=manifest.transport,
                manifest_sha256=manifest.fingerprint,
                evidence_ref=reference,
                evidence_sha256=sha256((canonical_json(proof) + "\n").encode()).hexdigest(),
                environment_fingerprint=env_hash,
                test_timestamp=stamp,
                boundaries=()
                if verdict == "skipped"
                else (
                    ("model", "wrapped_callable")
                    if probe_id == "premodel-deny"
                    else ("tool", "wrapped_callable")
                ),
                source="offline_behavioral",
                reason=reason,
            )
        )
    report = {
        **capability_report(manifest, observations),
        "mode": "offline",
        "test_timestamp": stamp,
        "environment": environment,
        "environment_fingerprint": env_hash,
        "probes": [item.to_dict() for item in observations],
        "network_used": False if type(driver) is OfflineDriver else None,
        "vendor_runtime_used": False if type(driver) is OfflineDriver else None,
        "driver_trust": "builtin_local_protocol"
        if type(driver) is OfflineDriver
        else "explicit_trusted_python_driver; network/process side effects not independently attested",
        "installed_sdk_probe_scope": "explicit adapter interface with protocol-compatible fakes",
        "live_features": "skipped; not inferred from fake requests",
        "status": "failed"
        if any(item.verdict == "verified_failed" for item in observations)
        else "passed"
        if any(item.verdict == "verified_supported" for item in observations)
        else "skipped",
    }
    return BenchResult(tuple(observations), canonical_json(report), canonical_json(proofs))
