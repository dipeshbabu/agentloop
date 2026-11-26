"""Independent scorers. Every gold answer comes from the public dataset itself."""

from __future__ import annotations

import collections
import os
import re
import string
import subprocess  # nosec B404 - runs dataset tests on generated code in an isolated child
import sys
import tempfile
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


def gsm8k_gold(answer: str) -> str:
    return answer.split("####")[-1].strip().replace(",", "")


def score_gsm8k(output: str | None, gold: str) -> float:
    """1.0 when the last number in the output equals the dataset's answer."""
    if not output:
        return 0.0
    numbers = _NUMBER.findall(str(output))
    if not numbers:
        return 0.0
    try:
        return float(Decimal(numbers[-1].replace(",", "").rstrip(".")) == Decimal(gold))
    except InvalidOperation:
        return 0.0


# SQuAD v2: the official evaluation's normalization, exact match and F1.
def _normalize(text: str) -> str:
    text = text.lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def _f1(prediction: str, gold: str) -> float:
    pred, ref = _normalize(prediction).split(), _normalize(gold).split()
    if not pred or not ref:
        return float(pred == ref)
    common = collections.Counter(pred) & collections.Counter(ref)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / len(pred), overlap / len(ref)
    return 2 * precision * recall / (precision + recall)


def score_squad(output: str | None, golds: list[str]) -> float:
    """Best F1 against the dataset's answers; unanswerable questions expect no answer."""
    prediction = (output or "").strip()
    if _normalize(prediction) in {"unanswerable", "no answer", "none", ""}:
        prediction = ""
    if not golds:
        return float(prediction == "")
    return max(_f1(prediction, gold) for gold in golds)


def score_humaneval(code: str | None, task: dict, *, timeout_s: float = 15.0) -> float:
    """Run HumanEval's own hidden tests on the code in a separate, isolated process.

    The child runs in an empty temporary directory with ``python -I`` and a
    minimal environment, and is killed at the timeout. Generated code is not
    trusted: this limits accidents, it is not a security sandbox.
    """
    if not code:
        return 0.0
    program = code + "\n\n" + task["test"] + f"\n\ncheck({task['entry_point']})\n"
    with tempfile.TemporaryDirectory(prefix="humaneval-") as folder:
        path = os.path.join(folder, "candidate.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(program)
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        try:
            result = subprocess.run(  # nosec B603 - fixed interpreter and file, no shell
                [sys.executable, "-I", path],
                cwd=folder,
                env=env,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 0.0
    return float(result.returncode == 0)
