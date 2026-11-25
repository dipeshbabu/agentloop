from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentloop.aggregate_cli import aggregate_app
from agentloop.client import AgentLoopClient
from agentloop.costs import format_cost_usd
from agentloop.drift_cli import drift_app
from agentloop.harness_bench.cli import harness_app
from agentloop.improvement_cli import improve_app
from agentloop.integrations.harbor.cli import harbor_app
from agentloop.integrations.hermes_cli import hermes_app
from agentloop.integrations.omnigent.cli import omnigent_app
from agentloop.interoperability.cli import telemetry_app
from agentloop.otel import trace_from_otel
from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    load_quality_fixtures,
)
from agentloop.store import (
    LABEL_KEY_PATTERN,
    MAX_LABEL_VALUE_LENGTH,
)
from agentloop.tracer import AgentTrace

app = typer.Typer(help="AgentLoop profiler CLI")
console = Console()


def _utf8_output() -> None:
    """Write UTF-8 when output is redirected on a legacy code page.

    On Windows, piped or redirected output uses the ANSI code page (for example
    cp1252). Task IDs, trace names and other user text outside that code page
    then raise UnicodeEncodeError mid-command and hide the real result.
    Interactive consoles already use UTF-8 and are left unchanged.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding in {"utf8", "utf_8"} or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


@app.callback()
def _main() -> None:
    """AgentLoop profiler CLI"""
    _utf8_output()


study_app = typer.Typer(help="Summarize offline studies and preserved intervention evidence.")
app.add_typer(study_app, name="study")
app.add_typer(aggregate_app, name="aggregate")
app.add_typer(drift_app, name="drift")
app.add_typer(harbor_app, name="harbor")
app.add_typer(hermes_app, name="hermes")
app.add_typer(telemetry_app, name="telemetry")
app.add_typer(omnigent_app, name="omnigent")
app.add_typer(harness_app, name="harness")
app.add_typer(improve_app, name="improve")


def _remote_client(api_url: str, api_key: str | None) -> AgentLoopClient:
    resolved_api_key = api_key if api_key is not None else os.getenv("AGENTLOOP_API_KEY")
    return AgentLoopClient(base_url=api_url, api_key=resolved_api_key)


def _remote_admin_client(api_url: str, admin_api_key: str | None) -> AgentLoopClient:
    resolved_admin_key = (
        admin_api_key if admin_api_key is not None else os.getenv("AGENTLOOP_ADMIN_API_KEY")
    )
    return AgentLoopClient(base_url=api_url, admin_api_key=resolved_admin_key)


def _read_json_input(path: Path, *, param_hint: str) -> object:
    try:
        exists = path.exists()
        is_file = path.is_file() if exists else False
    except OSError as exc:
        raise typer.BadParameter(
            f"Input file is not readable: {path} ({exc})", param_hint=param_hint
        ) from exc

    if not exists:
        raise typer.BadParameter(
            f"Input file does not exist: {path}. "
            "Generate synthetic traces explicitly with `agentloop demo` or `agentloop demo-all`.",
            param_hint=param_hint,
        )
    if not is_file:
        raise typer.BadParameter(f"Input path is not a regular file: {path}", param_hint=param_hint)

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise typer.BadParameter(
            f"Input file is not readable as UTF-8: {path} ({exc})", param_hint=param_hint
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"Input file is not valid JSON: {path} ({exc.msg} at line {exc.lineno}, "
            f"column {exc.colno})",
            param_hint=param_hint,
        ) from exc


def _write_json(out: Path, payload: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_trace(
    path: Path,
    *,
    otel: bool = False,
    name: str | None = None,
    param_hint: str = "path",
) -> AgentTrace:
    payload = _read_json_input(path, param_hint=param_hint)
    format_name = "OpenTelemetry" if otel else "AgentLoop"
    try:
        if otel:
            if not isinstance(payload, (dict, list)):
                raise ValueError("expected a JSON object or array")
            return trace_from_otel(payload, name=name)
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return AgentTrace.from_dict(payload)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise typer.BadParameter(
            f"Input file is not a valid {format_name} trace: {path} ({exc})",
            param_hint=param_hint,
        ) from exc


def _build_quality_report_from_file(
    path: Path,
    *,
    param_hint: str,
    baseline_trace: AgentTrace | None = None,
    candidate_trace: AgentTrace | None = None,
    min_score: float | None = None,
) -> dict:
    try:
        fixtures = load_quality_fixtures(path)
        return build_quality_report(
            fixtures,
            baseline_trace=baseline_trace,
            candidate_trace=candidate_trace,
            min_score=min_score,
        )
    except (OSError, UnicodeError, QualityValidationError) as exc:
        raise typer.BadParameter(
            f"Invalid quality fixture file: {path} ({exc})",
            param_hint=param_hint,
        ) from exc


def _print_value_summary(value: dict) -> None:
    monthly = value["monthly_value"]
    per_run = value["per_run"]
    reliability = value["reliability"]
    pricing = value.get("pricing", {})
    table = Table(title="AgentLoop Value Report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row(
        "Total monthly value", format_cost_usd(monthly.get("total_value_usd"), decimals=2)
    )
    table.add_row(
        "Model cost savings / month",
        format_cost_usd(monthly.get("direct_model_cost_savings_usd"), decimals=2),
    )
    table.add_row("Engineering hours saved / month", str(monthly["engineering_hours_saved"]))
    table.add_row("Latency saved / run", f"{per_run['latency_savings_ms'] / 1000:.3f}s")
    table.add_row("Cost saved / run", format_cost_usd(per_run.get("cost_savings_usd"), decimals=6))
    table.add_row("Reliability risk score", f"{reliability['risk_score']}/100")
    if pricing:
        table.add_row("Suggested plan", str(pricing.get("suggested_plan") or "unavailable"))
        table.add_row(
            "Suggested monthly price",
            format_cost_usd(pricing.get("suggested_monthly_price_usd"), decimals=0),
        )
        ratio = pricing.get("value_to_price_ratio")
        table.add_row("Value / price ratio", "n/a" if ratio is None else f"{ratio}x")
    console.print(table)
    console.print(value["value_summary"])


def _print_doctor(result: dict) -> None:
    table = Table(title="AgentLoop Doctor")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Detail")
    table.add_column("Fix")
    for check in result["checks"]:
        status = check["status"]
        style = "green" if status == "ok" else "yellow" if status == "warn" else "red"
        table.add_row(
            f"[{style}]{status}[/{style}]", check["name"], check["detail"], check.get("fix", "")
        )
    console.print(table)
    if result["failed"]:
        raise typer.Exit(1)


def _parse_labels(values: list[str] | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values or []:
        key, separator, text = value.partition("=")
        if not separator or not LABEL_KEY_PATTERN.match(key):
            raise typer.BadParameter(
                "Use KEY=VALUE with a key of letters, digits, '.', '_' or '-'",
                param_hint="--label",
            )
        if len(text) > MAX_LABEL_VALUE_LENGTH:
            raise typer.BadParameter(
                f"Label values are limited to {MAX_LABEL_VALUE_LENGTH} characters",
                param_hint="--label",
            )
        labels[key] = text
    return labels


def _upload_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(item for item in path.glob("*.json") if item.is_file())
            if not found:
                raise typer.BadParameter(
                    f"No .json trace files in directory {path}", param_hint="--path"
                )
            files.extend(found)
        else:
            files.append(path)
    return files


# Command groups register on ``app`` when imported. The order is the order
# --help lists them in, so keep it (#407).
# isort: off
from agentloop import cli_onboarding  # noqa: E402, F401
from agentloop import cli_analysis  # noqa: E402, F401
from agentloop import cli_setup  # noqa: E402, F401
from agentloop import cli_store  # noqa: E402, F401
from agentloop import cli_remote  # noqa: E402, F401
from agentloop import cli_runtime  # noqa: E402, F401
# isort: on

if __name__ == "__main__":
    app()
