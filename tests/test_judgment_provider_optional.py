"""Explicit opt-in contract probe for a host-owned provider adapter factory."""

from __future__ import annotations

import importlib
import os

import pytest
from workflow_fixtures import pipeline

from agentloop import JudgmentSession, JudgmentSpec, judgment_request
from agentloop.judgments import validate_judgment_record


def test_optional_host_owned_judgment_backend():
    if os.environ.get("AGENTLOOP_RUN_JUDGMENT_PROVIDER_TESTS") != "1":
        pytest.skip("external judgment tests require explicit opt-in")
    reference = os.environ.get("AGENTLOOP_JUDGMENT_TEST_FACTORY", "")
    module, separator, name = reference.partition(":")
    if (
        not separator
        or not all(part.isidentifier() for part in module.split("."))
        or not name.isidentifier()
    ):
        pytest.fail("set AGENTLOOP_JUDGMENT_TEST_FACTORY to a trusted module:factory")
    backend = getattr(importlib.import_module(module), name)()
    request = judgment_request(
        pipeline(),
        JudgmentSpec("Do these approved summaries match?", "boolean"),
        ["classify", "priority"],
        summaries={"classify": "same synthetic label", "priority": "same synthetic label"},
    )
    result = JudgmentSession(backend, cache_size=0).evaluate(request, enabled=True, timeout_s=15)
    validate_judgment_record(result)
    # This probes the adapter contract, not the independent quality of a model.
    assert result["evaluation"]["status"] in {"known", "unknown"}
