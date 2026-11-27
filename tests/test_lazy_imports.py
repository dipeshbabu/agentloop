"""The package root and the tracer must stay cheap to import."""

import subprocess
import sys
import textwrap

import agentloop

# Modules an application that only records traces should never pay for.
HEAVY = (
    "agentloop.metrics",
    "agentloop.rules",
    "agentloop.client",
    "agentloop.doctor",
    "agentloop.store",
)


def _fresh(code: str) -> str:
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_importing_the_package_or_tracer_loads_no_analysis_modules():
    loaded = _fresh(
        f"""
        import sys
        import agentloop, agentloop.tracer
        print(sorted(m for m in {HEAVY!r} if m in sys.modules))
        """
    )
    assert loaded == "[]"


def test_tracing_still_applies_environment_configuration():
    out = _fresh(
        """
        import os
        os.environ["AGENTLOOP_PROJECT_ID"] = "from-env"
        from agentloop.tracer import trace_agent
        import agentloop
        print(agentloop.get_runtime_config().project_id)
        """
    )
    assert out == "from-env"


def test_every_public_name_resolves_and_is_listed():
    for name in agentloop.__all__:
        assert getattr(agentloop, name) is not None, name
    assert set(agentloop.__all__) <= set(dir(agentloop))


def test_star_import_and_unknown_names():
    namespace: dict = {}
    exec("from agentloop import *", namespace)  # nosec B102
    assert {"trace_agent", "AgentLoopClient", "__version__"} <= set(namespace)
    try:
        agentloop.not_a_real_name
    except AttributeError as exc:
        assert "not_a_real_name" in str(exc)
    else:
        raise AssertionError("unknown attribute did not raise")


def test_report_still_works_from_a_trace():
    from agentloop.tracer import trace_agent, trace_model_call

    with trace_agent("lazy") as trace:
        with trace_model_call("plan", input_tokens=5, output_tokens=2):
            pass
    assert trace.report()["model_call_count"] == 1
