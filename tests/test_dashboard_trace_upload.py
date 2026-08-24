"""Unit tests for the uploaded-trace parsing helper.

``AppTest`` can only assert that *an* error was shown; these pin the message each of
the three rejection layers produces, and pin that a parse position is claimed for
malformed JSON alone.
"""

from __future__ import annotations

import json

import pytest

from agentloop.tracer import AgentTrace
from dashboard.trace_upload import TraceUploadError, parse_uploaded_trace


def test_a_valid_trace_round_trips() -> None:
    trace = AgentTrace(name="uploaded")

    parsed = parse_uploaded_trace(json.dumps(trace.to_dict()).encode("utf-8"))

    assert parsed.name == "uploaded"
    assert parsed.run_id == trace.run_id


def test_bytes_that_are_not_utf8_name_the_offending_byte() -> None:
    with pytest.raises(TraceUploadError) as caught:
        parse_uploaded_trace(b'{"name": "\xff"}')

    message = str(caught.value)
    assert "not UTF-8" in message
    assert "byte 10" in message
    # A decode failure has no line/column -- claiming one would point at nothing.
    assert "line" not in message


def test_an_empty_file_is_reported_as_malformed_json() -> None:
    """Uploading a 0-byte file is a click away in any file picker.

    Empty bytes are valid UTF-8, so this falls through the decode layer and fails as
    JSON at the first character -- which is a real position, not a fabricated one.
    """

    with pytest.raises(TraceUploadError) as caught:
        parse_uploaded_trace(b"")

    message = str(caught.value)
    assert "line 1" in message
    assert "column 1" in message


def test_malformed_json_reports_line_and_column() -> None:
    with pytest.raises(TraceUploadError) as caught:
        parse_uploaded_trace(b'{\n  "name": "broken"\n')

    message = str(caught.value)
    assert "line 3" in message
    assert "column 1" in message


def test_json_that_is_not_an_object_names_the_type_it_got() -> None:
    """``json.loads`` returns a list here, and ``from_dict`` would fail on subscripting."""

    with pytest.raises(TraceUploadError) as caught:
        parse_uploaded_trace(b"[]")

    assert "must be a JSON object, not list" in str(caught.value)


def test_a_schema_violation_is_reported_without_a_parse_position() -> None:
    with pytest.raises(TraceUploadError) as caught:
        parse_uploaded_trace(json.dumps({"not": "a trace"}).encode("utf-8"))

    message = str(caught.value)
    assert message
    # Valid JSON located a field, not a character -- no position to report.
    assert "line" not in message
    assert "column" not in message


def test_every_rejection_is_a_value_error() -> None:
    """Matching ``TraceValidationError``, so a broad handler upstream still catches."""

    assert issubclass(TraceUploadError, ValueError)
