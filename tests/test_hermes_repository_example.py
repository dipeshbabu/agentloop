from __future__ import annotations

import json

import pytest

from agentloop.quality import score_output
from examples.hermes_repository_runner import check_repository_answer

SOURCE = """[project]
name = "owned-example"
requires-python = ">=3.11"
authors = [{ name = "Example Maintainer" }]
"""


@pytest.mark.parametrize(
    ("field", "expected"),
    [("name", "owned-example"), ("requires-python", ">=3.11"), ("authors", "Example Maintainer")],
)
def test_value_check_repairs_from_the_source_without_expected_task_labels(field, expected):
    result, facts = check_repository_answer(
        SOURCE, f"What is the value in project.{field}?", '{"answer":"incorrect"}'
    )
    assert json.loads(result) == {"answer": expected}
    assert facts["model_answer_valid"] is False
    assert facts["repair_applied"] is True


def test_value_check_keeps_a_correct_model_response():
    response = '{"answer": "owned-example"}'
    result, facts = check_repository_answer(SOURCE, "What is project.name?", response)
    assert result == response
    assert facts["model_answer_valid"] is True
    assert facts["repair_applied"] is False


def test_metadata_example_rejects_a_question_without_a_source_field():
    with pytest.raises(ValueError, match="metadata field"):
        check_repository_answer(SOURCE, "Unscoped question", '{"answer":"anything"}')


def test_source_value_check_keeps_the_external_quality_criterion_independent():
    changed = SOURCE.replace("owned-example", "changed-upstream")
    result, _ = check_repository_answer(changed, "What is project.name?", '{"answer":"incorrect"}')
    assert json.loads(result) == {"answer": "changed-upstream"}
    fixture = {"expected": {"answer": "owned-example"}}
    assert score_output(result, fixture, {"type": "json_subset"})["passed"] is False
