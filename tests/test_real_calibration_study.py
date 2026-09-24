from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

from examples.real_calibration_study import (
    MODELS,
    PROMPT,
    code_hashes,
    normalize_answer,
    run_condition,
)


@pytest.mark.parametrize(
    "value",
    [
        True,
        None,
        "nan",
        "1e999999999",
        "1,23",
        "9" * 100,
        "0.00000000000000000000000001",
        "number: 4",
    ],
)
def test_numeric_scorer_rejects_invalid_or_unbounded_answers(value):
    assert normalize_answer(value) is None


def test_numeric_scorer_does_not_round_different_answers_together():
    assert normalize_answer("+0004.00") == "4"
    assert normalize_answer("1,234.50") == "1234.5"
    assert normalize_answer("-0.0") == "0"
    assert normalize_answer("4.000000000000000000000000000000000001") != "4"


def test_one_prediction_is_archived_before_the_candidate_and_remains_unchanged(
    tmp_path, monkeypatch
):
    import examples.real_agent_study.api as api
    import examples.real_calibration_study as study

    protocol = {
        "schema_version": "1.0",
        "workload": "offline calibration fixture",
        "models": MODELS,
        "code_hashes": code_hashes(),
        "configuration": {"prompt": PROMPT},
        "tasks": [{"id": "answer-00", "split": "fit", "prompt": "2+2", "expected": "4"}],
        "intervention": {"type": "model_routing", "configuration": {"scope": "single model call"}},
    }
    (tmp_path / "protocol.json").write_text(json.dumps(protocol))
    monkeypatch.setattr(study, "verify_server", lambda *args: None)
    results = iter(["4", "5"])

    def response(*args, **kwargs):
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": json.dumps({"answer": next(results)})},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", response)
    run_condition(tmp_path, "baseline", 0, "http://127.0.0.1:8766", "model.gguf")
    before = tmp_path / "baseline/0/answer-00"
    prediction = (before / "prediction.json").read_bytes()
    run_condition(tmp_path, "candidate", 0, "http://127.0.0.1:8766", "model.gguf")
    after = tmp_path / "candidate/0/answer-00"
    record = json.loads((after / "intervention.json").read_text())
    assert len(record["predicted"]["findings"]) == 1
    assert record["measured"]["quality"]["candidate_score"] == 0
    assert (before / "prediction.json").read_bytes() == prediction
    archived = json.loads((before / "journal.json").read_text())["prediction_recorded_at"]
    started = json.loads((after / "trace.json").read_text())["started_at"]
    assert datetime.fromisoformat(archived) < datetime.fromisoformat(started)
    with pytest.raises(ValueError, match="already exists"):
        run_condition(tmp_path, "candidate", 0, "http://127.0.0.1:8766", "model.gguf")


def test_candidate_cannot_precede_original_prediction(tmp_path, monkeypatch):
    import examples.real_calibration_study as study

    protocol = {
        "models": MODELS,
        "code_hashes": code_hashes(),
        "configuration": {"prompt": PROMPT},
        "tasks": [{"id": "answer-00", "split": "fit", "prompt": "2+2", "expected": "4"}],
    }
    (tmp_path / "protocol.json").write_text(json.dumps(protocol))
    monkeypatch.setattr(study, "verify_server", lambda *args: None)
    with pytest.raises(FileNotFoundError):
        run_condition(tmp_path, "candidate", 0, "http://127.0.0.1:8766", "model.gguf")
