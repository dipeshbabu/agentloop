"""A small question-answering agent with no AgentLoop code: the pilot's starting point.

It answers questions about a passage: it rewrites the question as a search
query, looks up sentences for both the question and the rewrite, answers from
what it found, then asks the model to double-check the answer.

    python support_agent.py questions.json
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

import agentloop

URL = "http://127.0.0.1:8766/v1/chat/completions"
WORD = re.compile(r"[a-z0-9]+")


@agentloop.traceable(kind="model", model="qwen3-4b-local")
def chat(messages, max_tokens=64):
    body = json.dumps({"messages": messages, "temperature": 0, "max_tokens": max_tokens}).encode()
    request = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)["choices"][0]["message"]["content"].strip()


@agentloop.traceable(kind="tool")
def lookup(passage, query, k=3):
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", passage) if s.strip()]
    terms = set(WORD.findall(query.lower()))
    ranked = sorted(sentences, key=lambda s: -len(terms & set(WORD.findall(s.lower()))))
    return ranked[:k]


def answer(passage, question):
    found = lookup(passage, question)
    draft = chat(
        [
            {
                "role": "system",
                "content": "Answer with the shortest span from the text, or unanswerable.",
            },
            {"role": "user", "content": f"Text: {' '.join(found)}\n\nQuestion: {question}"},
        ]
    )
    return chat(
        [
            {
                "role": "system",
                "content": "Check the answer against the text. Reply with the corrected "
                "shortest span, or unanswerable.",
            },
            {
                "role": "user",
                "content": f"Text: {passage}\n\nQuestion: {question}\nAnswer: {draft}",
            },
        ]
    )


def main(path, trace_out):
    with agentloop.trace_agent("support_agent") as trace:
        for item in json.load(open(path, encoding="utf-8")):
            print(
                json.dumps({"id": item["id"], "answer": answer(item["passage"], item["question"])})
            )
    trace.export_json(trace_out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
