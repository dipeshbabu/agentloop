"""Three agents written the way developers commonly write them, plus a control.

Each agent takes a task (without its gold answer), a configuration and a model,
and returns its final output. Every model call, tool call and retry is recorded
in the active AgentLoop trace. The configuration knobs are what optimization
methods may change; DEFAULTS are the baselines, frozen before any run.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import re
import subprocess  # nosec B404 - runs the prompt's own examples in an isolated child
import sys
import tempfile
import threading
from typing import Any

from agentloop.tracer import trace_retry, trace_tool_call

DEFAULTS: dict[str, dict[str, Any]] = {
    "math": {"history": "full", "reuse_calculations": False, "verify_answer": True,
             "max_steps": 6, "max_parse_retries": 2},
    "extraction": {"rephrase_retrieval": True, "parallel_retrieval": False, "verify_answer": True},
    "coding": {"self_test": True, "max_repairs": 3, "verify_answer": True},
    "extraction_single": {},
}  # fmt: skip


def _json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# Math (GSM8K) ----------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}  # fmt: skip


def calculate(expression: str) -> float:
    tree = ast.parse(expression.replace("^", "**"), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            return -visit(node.operand) if isinstance(node.op, ast.USub) else visit(node.operand)
        raise ValueError("unsupported expression")

    value = visit(tree.body)
    if not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError("result out of range")
    return value


MATH_SYSTEM = (
    "You solve grade-school math word problems step by step using a calculator.\n"
    'Reply with exactly one JSON object: {"action": "calculate", "expression": "..."} '
    'to evaluate arithmetic, or {"action": "answer", "value": <number>} when done.'
)


def run_math(task: dict, config: dict, model) -> str | None:
    question = task["question"]
    history = [{"role": "system", "content": MATH_SYSTEM}, {"role": "user", "content": question}]
    last_result: str | None = None
    calculations: dict[str, str] = {}
    answer = None
    for step in range(config["max_steps"]):
        if config["history"] == "full":
            messages = history
        else:
            messages = history[:2] + (
                [{"role": "user", "content": last_result}] if last_result else []
            )
        decision = None
        for attempt in range(config["max_parse_retries"] + 1):
            if attempt == 0:
                text = model.chat("math.decide", messages, max_tokens=160, json_mode=True)
            else:
                with trace_retry(
                    "math.decide.retry", metadata={"retry_reason": "invalid_output_schema"}
                ):
                    text = model.chat("math.decide", messages, max_tokens=160, json_mode=True)
            decision = _json(text)
            if decision and decision.get("action") in {"calculate", "answer"}:
                break
            decision = None
        if decision is None:
            break
        history.append({"role": "assistant", "content": json.dumps(decision)})
        if decision["action"] == "answer":
            answer = str(decision.get("value"))
            break
        expression = str(decision.get("expression", ""))[:300]
        metadata = {
            "read_only": True,
            "tool_reuse_key": "calc:" + expression,
            "agentloop.effects": {"effect": "none", "deterministic": True, "idempotent": True},
            "depends_on": [],
        }
        if config["reuse_calculations"] and expression in calculations:
            result = calculations[expression]
        else:
            with trace_tool_call("calculator", metadata=metadata):
                try:
                    result = f"Result: {calculate(expression):g}"
                except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as error:
                    result = f"Error: {error}"
            calculations[expression] = result
        last_result = f"{expression} -> {result}"
        history.append({"role": "user", "content": last_result})
    if answer is not None and config["verify_answer"]:
        check = model.chat(
            "math.verify",
            [{"role": "system", "content": "Check the proposed answer to the problem. "
              'Reply with JSON {"value": <number>} containing the correct final answer.'},
             {"role": "user", "content": f"Problem: {question}\nProposed answer: {answer}"}],
            max_tokens=200, json_mode=True,
        )  # fmt: skip
        verified = _json(check)
        if verified and "value" in verified:
            answer = str(verified["value"])
    return answer


# Extraction (SQuAD v2) --------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def find_sentences(context: str, query: str, k: int = 3) -> list[str]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", context) if s.strip()]
    terms = set(_WORD.findall(query.lower())) - {"the", "a", "an", "of", "in", "what", "is", "was"}
    scored = sorted(
        ((len(terms & set(_WORD.findall(s.lower()))), -i, s) for i, s in enumerate(sentences)),
        reverse=True,
    )
    return [s for score, _, s in scored[:k] if score > 0]


def _retrieve(context: str, query: str, label: str) -> list[str]:
    metadata = {
        "read_only": True,
        "tool_reuse_key": "find:" + query[:100],
        "agentloop.effects": {"effect": "none", "deterministic": True, "idempotent": True},
        "depends_on": [],
    }
    with trace_tool_call(label, metadata=metadata):
        return find_sentences(context, query)


ANSWER_SYSTEM = (
    "Answer the question using only the given text. Reply with the shortest exact span "
    "from the text that answers it, or with the single word unanswerable if the text "
    "does not contain the answer."
)


def run_extraction(task: dict, config: dict, model) -> str:
    context, question = task["context"], task["question"]
    queries = [question]
    if config["rephrase_retrieval"]:
        rephrased = model.chat(
            "extract.rephrase",
            [
                {
                    "role": "user",
                    "content": f"Rewrite this question as a short search query: {question}",
                }
            ],
            max_tokens=40,
        ).strip()
        queries.append(rephrased or question)
    found: list[str] = []
    if config["parallel_retrieval"] and len(queries) > 1:
        results: dict[int, list[str]] = {}
        threads = [
            threading.Thread(
                target=lambda i=i, q=q: results.__setitem__(
                    i, _retrieve(context, q, "find_sentences")
                )
            )
            for i, q in enumerate(queries)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for i in range(len(queries)):
            found += [s for s in results[i] if s not in found]
    else:
        for query in queries:
            found += [s for s in _retrieve(context, query, "find_sentences") if s not in found]
    passage = " ".join(found) if found else context
    answer = model.chat(
        "extract.answer",
        [{"role": "system", "content": ANSWER_SYSTEM},
         {"role": "user", "content": f"Text: {passage}\n\nQuestion: {question}"}],
        max_tokens=48,
    ).strip()  # fmt: skip
    if config["verify_answer"]:
        checked = model.chat(
            "extract.verify",
            [{"role": "system", "content": "Check whether the answer is supported by the text. "
              "Reply with the corrected shortest span, or unanswerable."},
             {"role": "user", "content": f"Text: {context}\n\nQuestion: {question}\nAnswer: {answer}"}],
            max_tokens=48,
        ).strip()  # fmt: skip
        answer = checked or answer
    return answer


def run_extraction_single(task: dict, config: dict, model) -> str:
    """Negative control: one model call, nothing for a configuration change to remove."""
    return model.chat(
        "extract.answer",
        [{"role": "system", "content": ANSWER_SYSTEM},
         {"role": "user", "content": f"Text: {task['context']}\n\nQuestion: {task['question']}"}],
        max_tokens=48,
    ).strip()  # fmt: skip


# Coding (HumanEval) --------------------------------------------------------------------

_CODE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _code(text: str) -> str:
    match = _CODE.search(text or "")
    return (match.group(1) if match else text or "").strip()


def prompt_examples(prompt: str) -> list[tuple[str, str]]:
    """The prompt's own >>> examples: public, part of the task, not the hidden tests."""
    lines = prompt.splitlines()
    examples = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">>>") and index + 1 < len(lines):
            expected = lines[index + 1].strip()
            if expected and not expected.startswith(">>>"):
                examples.append((stripped[3:].strip(), expected))
    return examples


def run_examples(code: str, examples: list[tuple[str, str]], timeout_s: float = 10.0) -> str | None:
    """None when every example passes, else the first failure, in an isolated child."""
    checks = "\n".join(
        f"_r = {expr}\nassert repr(_r) == {expected!r} or str(_r) == {expected!r}, "
        f"'{expr} returned ' + repr(_r) + ', expected ' + {expected!r}"
        for expr, expected in examples
    )
    with tempfile.TemporaryDirectory(prefix="selftest-") as folder:
        path = os.path.join(folder, "candidate.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(code + "\n\n" + checks + "\n")
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        try:
            result = subprocess.run(  # nosec B603 - fixed interpreter and file, no shell
                [sys.executable, "-I", path], cwd=folder, env=env,
                capture_output=True, text=True, timeout=timeout_s, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return "timed out"
    if result.returncode == 0:
        return None
    return (result.stderr.strip().splitlines() or ["failed"])[-1][:300]


CODE_SYSTEM = (
    "Complete the Python function. Reply with the full function in one ```python code block."
)


def _with_prefix(prompt: str, entry_point: str, code: str) -> str:
    """Keep the prompt's imports and helper functions, which replies often omit."""
    marker = f"def {entry_point}("
    prefix = prompt.split(marker)[0] if marker in prompt else ""
    if not prefix.strip() or prefix.strip() in code:
        return code
    return prefix.rstrip() + "\n\n\n" + code


def run_coding(task: dict, config: dict, model) -> str:
    prompt = task["prompt"]
    code = _code(model.chat(
        "code.generate",
        [{"role": "system", "content": CODE_SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=400,
    ))  # fmt: skip
    code = _with_prefix(prompt, task["entry_point"], code)
    examples = prompt_examples(prompt) if config["self_test"] else []
    repairs = 0
    while examples:
        metadata = {
            "read_only": True,
            "agentloop.effects": {"effect": "none", "deterministic": True},
        }
        with trace_tool_call("run_examples", metadata=metadata):
            failure = run_examples(code, examples)
        if failure is None or repairs >= config["max_repairs"]:
            break
        repairs += 1
        with trace_retry("code.repair", metadata={"retry_reason": "verification_failed"}):
            code = _code(model.chat(
                "code.repair",
                [{"role": "system", "content": CODE_SYSTEM},
                 {"role": "user", "content": f"{prompt}\n\nYour code:\n```python\n{code}\n```\n"
                  f"failed this example: {failure}\nFix it."}],
                max_tokens=400,
            ))  # fmt: skip
            code = _with_prefix(prompt, task["entry_point"], code)
    if config["verify_answer"]:
        reviewed = _code(model.chat(
            "code.review",
            [{"role": "system", "content": "Review the function for bugs and reply with the final "
              "function in one ```python code block."},
             {"role": "user", "content": f"{prompt}\n\n```python\n{code}\n```"}],
            max_tokens=400,
        ))  # fmt: skip
        code = _with_prefix(prompt, task["entry_point"], reviewed) if reviewed else code
    return code


AGENTS = {
    "math": run_math,
    "extraction": run_extraction,
    "coding": run_coding,
    "extraction_single": run_extraction_single,
}
