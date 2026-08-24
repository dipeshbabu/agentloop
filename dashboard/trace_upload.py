"""Decode an uploaded trace file into an :class:`AgentTrace`, or say what to fix.

The Ingest page reads an uploaded file on every Streamlit rerun, so the three ways a
file can be unusable -- not UTF-8, not JSON, not a trace -- arrive as three exceptions
from three layers, each with its own idea of what a useful message looks like. Doing
that translation inline in ``app.py`` put the only copy of it inside a page that can
only be exercised through ``AppTest``. It lives here so it can be unit tested directly.
"""

from __future__ import annotations

import json
from typing import Any

from agentloop.schema import TraceValidationError
from agentloop.tracer import AgentTrace


class TraceUploadError(ValueError):
    """An uploaded trace the operator can fix, carrying the reason it was rejected.

    ``str(exc)`` is the operator-facing clause and nothing else -- no prefix, no
    trailing period -- so a caller can place it in whatever sentence its surface
    needs. It subclasses :class:`ValueError`, matching
    :class:`~agentloop.schema.TraceValidationError`.
    """


def parse_uploaded_trace(payload: bytes) -> AgentTrace:
    """Return the trace in ``payload``, or raise :class:`TraceUploadError` saying why not.

    A parse position is reported only for malformed JSON, which is the only failure
    that has one: a decode error is located by byte offset, and a schema violation is
    located by field path.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TraceUploadError(
            f"the file is not UTF-8 text ({exc.reason} at byte {exc.start})"
        ) from exc

    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TraceUploadError(f"{exc.msg} at line {exc.lineno}, column {exc.colno}") from exc

    if not isinstance(document, dict):
        # json.loads happily returns a list, string or number, and from_dict would
        # then fail on subscripting rather than on the schema.
        raise TraceUploadError(f"a trace must be a JSON object, not {type(document).__name__}")

    try:
        return AgentTrace.from_dict(document)
    except TraceValidationError as exc:
        raise TraceUploadError(str(exc)) from exc
