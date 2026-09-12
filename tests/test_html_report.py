from __future__ import annotations

import copy
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import _analysis_payload, _quickstart_trace, app
from agentloop.html_report import analysis_to_html
from agentloop.schema import TraceValidationError
from agentloop.tracer import AgentTrace


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for key, value in attrs:
            assert not key.lower().startswith("on")
            assert key not in {"src", "srcdoc"}
            if key == "href":
                self.links.append(value)
            if key == "id":
                self.ids.append(value)


def snapshot_trace(case):
    trace = _quickstart_trace()
    if case == "empty":
        return AgentTrace(
            name="empty",
            run_id="empty-run",
            started_at=trace.started_at,
            ended_at=trace.started_at,
            elapsed_ms=0,
        )
    if case == "special":
        trace.name = '<script>alert("trace")</script> & report'
        trace.metadata["unsafe"] = '</pre><img src=x onerror="alert(1)">'
        trace.events[0].name = 'tool <svg onload="alert(1)">'
        trace.events[0].event_id = 'span" onclick="alert(1)'
        trace.events[0].input_text = "raw private content"
    if case == "many":
        originals = list(trace.events)
        for repeat in range(5):
            for event in originals:
                duplicate = copy.deepcopy(event)
                duplicate.event_id += f"-repeat{repeat}"
                duplicate.name += f"-repeat{repeat}"
                trace.events.append(duplicate)
    return trace


@pytest.mark.parametrize("case", ["empty", "special", "many"])
def test_html_golden_artifacts(case):
    result = analysis_to_html(_analysis_payload(snapshot_trace(case)))
    expected = Path(__file__).parent / "fixtures" / "html" / f"{case}.html"
    assert result == expected.read_text(encoding="utf-8")
    parser = ReportParser()
    parser.feed(result)
    assert "script" not in parser.tags
    assert "img" not in parser.tags
    assert "iframe" not in parser.tags
    assert len(set(parser.ids)) == len(parser.ids)
    assert all(link.startswith("#") and link[1:] in parser.ids for link in parser.links)
    assert "Content-Security-Policy" in result


def test_raw_event_content_is_opt_in_and_safe():
    payload = _analysis_payload(snapshot_trace("special"))
    hidden = analysis_to_html(payload)
    included = analysis_to_html(payload, include_content=True)
    assert "raw private content" not in hidden
    assert "raw private content" in included
    assert "Raw prompts, outputs, errors, and event metadata are included" in included
    assert "<script>alert" not in included
    assert "<img src=x" not in included
    ReportParser().feed(included)


def test_cli_generates_one_file_and_retains_optional_replay_quality(tmp_path):
    baseline = _quickstart_trace()
    candidate = copy.deepcopy(baseline)
    candidate.run_id = "candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    before, after, html_path, json_path, fixtures = [
        tmp_path / name
        for name in (
            "baseline.json",
            "candidate.json",
            "report.html",
            "report.json",
            "quality.json",
        )
    ]
    baseline.export_json(before)
    candidate.export_json(after)
    fixtures.write_text(
        json.dumps([{"expected": "ok", "baseline_output": "ok", "candidate_output": "bad"}]),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            str(after),
            "--baseline",
            str(before),
            "--html",
            str(html_path),
            "--json-out",
            str(json_path),
            "--quality-fixtures",
            str(fixtures),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["replay"]["quality"]["passed"] is False
    assert payload["replay"]["gates"]["passed"] is False
    document = html_path.read_text(encoding="utf-8")
    assert "Configured gates: <strong>failed" in document
    assert "Observed baseline / candidate comparison" in document
    assert "uncalibrated" in document
    assert "estimator_version" in document
    ReportParser().feed(document)


@pytest.mark.parametrize(
    "change", [{"schema_version": "2.0"}, {"events": "invalid"}, {"name": None}]
)
def test_malformed_or_unsupported_traces_do_not_generate_artifacts(tmp_path, change):
    payload = _quickstart_trace().to_dict()
    payload.update(change)
    source, output = tmp_path / "bad.json", tmp_path / "bad.html"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = CliRunner().invoke(app, ["analyze", str(source), "--html", str(output)])
    assert result.exit_code != 0
    assert not output.exists()


def test_renderer_rejects_unsupported_payload_boundaries():
    with pytest.raises(ValueError, match="combined"):
        analysis_to_html({})
    payload = _analysis_payload(_quickstart_trace())
    payload["trace"]["schema_version"] = "2.0"
    with pytest.raises(TraceValidationError):
        analysis_to_html(payload)


def test_incomplete_analysis_is_visible():
    payload = _analysis_payload(_quickstart_trace())
    payload["diagnosis"]["analysis_complete"] = False
    payload["diagnosis"]["rule_errors"] = [{"message": "<unsafe>"}]
    document = analysis_to_html(payload)
    assert "Analysis incomplete" in document
    assert "<unsafe>" not in document
