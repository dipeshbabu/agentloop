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


CLI_ENGINES = (
    *HEAVY,
    "agentloop.improvement_cli",
    "agentloop.aggregate_cli",
    "agentloop.drift_cli",
    "agentloop.html_report",
    "agentloop.ablations",
    "agentloop.calibration",
    "agentloop.findings",
)


def test_cli_startup_loads_no_engine_or_command_group():
    loaded = _fresh(
        f"""
        import sys
        import agentloop.entrypoint
        print(sorted(m for m in {CLI_ENGINES!r} if m in sys.modules))
        """
    )
    assert loaded == "[]"


def test_every_cli_command_and_group_still_loads():
    import typer
    import typer.main
    from typer.testing import CliRunner

    from agentloop.cli import LAZY_GROUPS
    from agentloop.entrypoint import app

    root = typer.main.get_command(app)
    context = typer.Context(root)
    paths = []

    def walk(command, prefix):
        if hasattr(command, "list_commands"):
            for name in command.list_commands(context):
                walk(command.get_command(context, name), [*prefix, name])
        paths.append(prefix)

    walk(root, [])
    top = [path[0] for path in paths if len(path) == 1]
    # Lazy groups keep their --help position: after every command and ``study``.
    assert top[-len(LAZY_GROUPS) - 1 :] == ["study", *LAZY_GROUPS]
    runner = CliRunner()
    for path in paths:
        result = runner.invoke(app, [*path, "--help"])
        assert result.exit_code == 0, (path, result.output)


def test_unknown_cli_command_is_still_an_error():
    from typer.testing import CliRunner

    from agentloop.entrypoint import app

    result = CliRunner().invoke(app, ["no-such-command"])
    assert result.exit_code == 2


def test_cli_client_stays_replaceable():
    from agentloop import cli
    from agentloop.client import AgentLoopClient

    assert cli.AgentLoopClient is AgentLoopClient
    assert cli._remote_client("http://127.0.0.1:1", "al_test").__class__ is AgentLoopClient


def test_bundlers_can_see_every_lazy_group():
    # PyInstaller finds modules by scanning import statements; module names in
    # strings are invisible to it. cli.py imports each lazy group under
    # TYPE_CHECKING so the standalone CLI bundles them.
    import ast
    from pathlib import Path

    from agentloop import cli
    from agentloop.cli import LAZY_GROUPS

    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert {module for module, _ in LAZY_GROUPS.values()} <= imported
