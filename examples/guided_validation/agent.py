"""Bounded fixture agents: repository navigation, retrieval and a quoting graph.

Offline mode is synthetic contract validation. --model-url uses actual local
inference through the existing study client; it never calls a paid provider.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "guided_validation"

from agentloop import trace_agent, trace_tool_call

if __package__.startswith("examples."):
    from examples.real_agent_study.api import LocalModel
    from examples.real_agent_study.run import MODELS
else:
    from real_agent_study.api import LocalModel
    from real_agent_study.run import MODELS

SOURCE = '''def retry_delay(attempt):
    """Cap exponential retry backoff at thirty seconds."""
    return min(2 ** attempt, 30)

def cache_key(project, request):
    """Namespace a request cache entry by project."""
    return project + ":" + request

def is_complete(status):
    """Recognize successfully completed execution."""
    return status == "completed"
'''
DOCUMENTS = {
    "retry-policy": "Retry only idempotent reads. The retry limit is 3 attempts.",
    "retention-policy": "Delete debug traces after 14 days. Keep public examples separately.",
    "context-policy": "The request context budget is 4096 tokens. Never truncate the final answer.",
}
CATALOG = {"book": 7, "pen": 2, "pad": 3}


class Agent:
    def __init__(self, workload, condition, *, traced=True, model=None, framework="python"):
        self.workload, self.condition = workload, condition
        self.traced, self.model = traced, model
        self.cache = {}
        self.graph = None
        if framework == "langgraph" and workload == "workflow":
            from langgraph.graph import END, START, StateGraph

            graph = StateGraph(dict)
            graph.add_node(
                "collect", lambda state: {**state, "context": self.context(state["input"])}
            )
            graph.add_node(
                "answer", lambda state: {"output": self.answer(state["input"], state["context"])}
            )
            graph.add_edge(START, "collect")
            graph.add_edge("collect", "answer")
            graph.add_edge("answer", END)
            self.graph = graph.compile()

    def lookup(self, payload):
        key = json.dumps(payload, sort_keys=True)
        if self.condition != "baseline" and key in self.cache:
            return self.cache[key]
        context = (
            trace_tool_call("read_" + self.workload, metadata={"parallel_safe": True})
            if self.traced
            else nullcontext()
        )
        with context:
            if self.workload == "repository":
                # Parse executable source independently from the held-out labels.
                tree = ast.parse(SOURCE)
                value = {node.name: ast.get_docstring(node) for node in tree.body}
            elif self.workload == "retrieval":
                value = {payload["document"]: DOCUMENTS[payload["document"]]}
            else:
                # A read-only tool result, followed by an independent graph answer node.
                value = {
                    "items": {sku: CATALOG[sku] for sku in payload["basket"]},
                    "basket": payload["basket"],
                }
            if self.condition != "baseline":
                self.cache[key] = value
            return value

    def context(self, payload):
        # Existing application bug: three layers request identical immutable data.
        for _ in range(3):
            value = self.lookup(payload)
        return value

    def answer(self, payload, context):
        if self.model:
            self.model.response_schema = {
                "type": "object",
                "properties": {"answer": {"type": "string"}, "source": {"type": "string"}},
                "required": ["answer", "source"],
                "additionalProperties": False,
            }
            prompt = {"question": payload["question"], "context": context, "source": self.workload}
            text = self.model.complete(
                [
                    {
                        "role": "system",
                        "content": "Answer using only the supplied context. Return JSON with string answer and source. For repository questions answer with the function name; for retrieval copy only the requested numeric value; for workflow compute total price times quantities. source is the supplied source string.",
                    },
                    {"role": "user", "content": json.dumps(prompt)},
                ],
                remaining_s=60,
            )
            output = json.loads(text)
        else:
            if self.workload == "repository":
                # Scripted fixture policy; this is explicitly not model evidence.
                word = payload["keyword"]
                answer = next(name for name, doc in context.items() if word in doc)
            elif self.workload == "retrieval":
                import re

                answer = re.search(r"\d+", next(iter(context.values()))).group()
            else:
                answer = str(
                    sum(context["items"][sku] * count for sku, count in payload["basket"].items())
                )
            output = {"answer": answer, "source": self.workload}
        if self.condition == "wrong":
            output["answer"] = "deliberately incorrect"
        return output

    def run(self, payload):
        self.cache.clear()  # Cache only within a request, never between study tasks.
        if self.graph:
            return self.graph.invoke({"input": payload})["output"]
        return self.answer(payload, self.context(payload))


def execute(request, *, condition, model_url=None, traced=True, framework="python"):
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    payload = request["input"]
    model = (
        LocalModel(model_url, MODELS["candidate"], max_tokens=96, timeout_s=60, seed=20261112)
        if model_url
        else None
    )
    agent = Agent(payload["workload"], condition, traced=traced, model=model, framework=framework)
    metadata = {
        "synthetic": model is None,
        "evidence_kind": "offline_contract_fixture"
        if model is None
        else "real_local_model_fixture_agent",
        "task_data": "owned_small_fixture",
        "framework": framework,
        "condition": condition,
    }
    trace_context = (
        trace_agent("guided-" + payload["workload"], metadata=metadata) if traced else nullcontext()
    )
    start = time.perf_counter()
    failure = None
    with trace_context as trace:
        if model:
            model.trace = trace
        try:
            output = agent.run(payload)
            if trace:
                trace.metadata["output"] = output
        except Exception as error:
            failure = error
            output = None
            if trace:
                trace.metadata.update(success=False, output_unavailable=True)
    elapsed = (time.perf_counter() - start) * 1000
    if trace:
        trace.export_json(request["trace_path"])
    return {
        "output": output,
        "elapsed_ms": elapsed,
        "error": type(failure).__name__ if failure else None,
        "model_receipts": model.receipts if model else [],
        "traced": traced,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=("baseline", "candidate", "wrong"), required=True)
    parser.add_argument("--model-url")
    parser.add_argument("--framework", choices=("python", "langgraph"), default="python")
    args = parser.parse_args()
    result = execute(
        json.load(sys.stdin),
        condition=args.condition,
        model_url=args.model_url,
        framework=args.framework,
    )
    if result["error"]:
        print(
            "Agent failed (" + result["error"] + "). Inspect the retained failed trace.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
