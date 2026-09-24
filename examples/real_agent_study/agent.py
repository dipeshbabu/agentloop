"""The same bounded tool loop runs as a real LangGraph or custom Python agent."""

from __future__ import annotations

import json
import time
from typing import TypedDict

from agentloop.events import utc_now_iso
from agentloop.tracer import record_tool_call

from .tools import RepositoryTools, WineDatabase, calculate

PROMPT_VERSION = "tool-agent-2.0"
COMMON = """Solve the user's task using the available read-only tools. You must use a relevant tool before answering. Return one JSON object per turn. To call a tool, use {"action":"TOOL_NAME","argument":ARGUMENT}. To finish, use {"action":"final","answer":FINAL_OBJECT}. Do not include markdown. Tool results arrive in the next user message. Never invent a tool result. You have at most four model calls, so finish promptly after obtaining evidence."""
INSTRUCTIONS = {
    "repository": 'Tools: search takes a literal substring string and returns matching source lines. read takes {"path":"agentloop/file.py","start":1,"count":30} with at most 60 lines. Available files: tracer.py, parallelism.py, costs.py, quality.py, replay.py, ci.py, interventions.py, studies.py, html_report.py, tokens.py under agentloop/. Final object: {"path":"agentloop/file.py","symbol":"function or Class.method","evidence_line":123,"evidence":"exact text copied from that source line"}. Quote a line within the named function. Search for relevant function names or words, then read the evidence if necessary.',
    "sql": 'Tool: sql takes a read-only SQLite SELECT string. Table wines contains color (red or white) and numeric columns fixed_acidity, volatile_acidity, citric_acid, residual_sugar, chlorides, free_sulfur_dioxide, total_sulfur_dioxide, density, ph, sulphates, alcohol, quality. Allowed functions: avg,sum,count,min,max,round,abs,coalesce,ifnull,total,length,lower,upper. Maximum 50 result rows. Final object: {"rows":[[value1,value2]]}, exactly matching the requested result and ordering. Use the SQL tool to compute the answer.',
    "math": 'Tool: calculate takes an arithmetic expression string using numeric literals and + - * / // % and parentheses. No variables, code, functions or exponentiation. Compute the required final value with the tool. Final object: {"answer":"numeric result"}. The final value must match a successful calculator result and the question\'s required quantity.',
}


def decision_schema(workload):
    def action(name, field, schema):
        return {
            "type": "object",
            "properties": {"action": {"const": name}, field: schema},
            "required": ["action", field],
            "additionalProperties": False,
        }

    if workload == "repository":
        tools = [
            action("search", "argument", {"type": "string"}),
            action(
                "read",
                "argument",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start": {"type": "integer"},
                        "count": {"type": "integer"},
                    },
                    "required": ["path", "start", "count"],
                    "additionalProperties": False,
                },
            ),
        ]
        answer = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "symbol": {"type": "string"},
                "evidence_line": {"type": "integer"},
                "evidence": {"type": "string"},
            },
            "required": ["path", "symbol", "evidence_line", "evidence"],
            "additionalProperties": False,
        }
    elif workload == "sql":
        tools = [action("sql", "argument", {"type": "string"})]
        answer = {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": ["string", "number", "null"]}},
                }
            },
            "required": ["rows"],
            "additionalProperties": False,
        }
    elif workload == "math":
        tools = [action("calculate", "argument", {"type": "string"})]
        answer = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
    else:
        raise ValueError("unknown workload")
    return {"oneOf": [*tools, action("final", "answer", answer)]}


class AgentState(TypedDict):
    messages: list[dict]
    calls: int
    pending: dict | None
    answer: dict | None
    status: str
    history: list[dict]


class ToolAgent:
    def __init__(self, workload, model, *, sources, red_csv, white_csv, max_calls, timeout_s):
        self.workload, self.model = workload, model
        self.model.response_schema = decision_schema(workload)
        self.max_calls, self.timeout_s = max_calls, timeout_s
        self.repository = RepositoryTools(sources) if workload == "repository" else None
        self.database = WineDatabase(red_csv, white_csv) if workload == "sql" else None
        self.tool_recording_ms = 0.0
        self.graph = None
        if workload == "repository":
            from langgraph.graph import END, START, StateGraph

            builder = StateGraph(AgentState)
            builder.add_node("decide", self.decide)
            builder.add_node("tool", self.tool)
            builder.add_edge(START, "decide")
            builder.add_conditional_edges(
                "decide",
                lambda state: "tool" if state["status"] == "running" else "end",
                {"tool": "tool", "end": END},
            )
            builder.add_edge("tool", "decide")
            self.graph = builder.compile()

    def decide(self, state):
        if state["calls"] >= self.max_calls or time.perf_counter() >= self.deadline:
            return {
                **state,
                "status": "call_limit" if state["calls"] >= self.max_calls else "timeout",
            }
        state = {**state, "messages": list(state["messages"]), "calls": state["calls"] + 1}
        try:
            text = self.model.complete(
                state["messages"], remaining_s=self.deadline - time.perf_counter()
            )
        except Exception as exc:
            return {
                **state,
                "status": "model_error",
                "history": [
                    *state["history"],
                    {"tool": "model", "status": "error", "error_category": type(exc).__name__},
                ],
            }
        state["messages"].append({"role": "assistant", "content": text})
        try:
            decision = json.loads(text)
            json.dumps(decision, allow_nan=False)
            if not isinstance(decision, dict) or not isinstance(decision.get("action"), str):
                raise ValueError("invalid decision object")
            if decision["action"] == "final":
                if not isinstance(decision.get("answer"), dict):
                    raise ValueError("final answer must be an object")
                return {
                    **state,
                    "status": "completed" if time.perf_counter() <= self.deadline else "timeout",
                    "answer": decision["answer"],
                }
            return {**state, "pending": decision}
        except (ValueError, TypeError):
            return {**state, "pending": {"action": "invalid", "argument": None}}

    def tool(self, state):
        decision = state["pending"]
        name, argument = decision["action"], decision.get("argument")
        started, start = utc_now_iso(), time.perf_counter()
        result, error = None, None
        try:
            if self.repository is not None and name == "search":
                result = self.repository.search(argument)
            elif self.repository is not None and name == "read":
                result = self.repository.read(argument)
            elif self.database is not None and name == "sql":
                result = self.database.query(argument)
            elif self.workload == "math" and name == "calculate":
                result = calculate(argument)
            else:
                raise ValueError("unknown or malformed tool request")
        except Exception as exc:
            error = type(exc).__name__
        duration = (time.perf_counter() - start) * 1000
        ended = utc_now_iso()
        receipt = {
            "tool": name,
            "argument": argument,
            "status": "error" if error else "ok",
            "error_category": error,
            "result": result,
            "duration_ms": duration,
        }
        if self.model.trace is not None:
            record_start = time.perf_counter()
            record_tool_call(
                name,
                trace=self.model.trace,
                duration_ms=duration,
                started_at=started,
                ended_at=ended,
                status=receipt["status"],
                error=error,
                metadata={"read_only": True, "workload": self.workload},
            )
            self.tool_recording_ms += (time.perf_counter() - record_start) * 1000
        feedback = {"tool": name, "result": result, "error": error}
        return {
            **state,
            "pending": None,
            "history": [*state["history"], receipt],
            "messages": [
                *state["messages"],
                {"role": "user", "content": json.dumps(feedback, ensure_ascii=False)},
            ],
        }

    def run(self, prompt):
        self.deadline = time.perf_counter() + self.timeout_s
        state = {
            "messages": [
                {"role": "system", "content": COMMON + "\n" + INSTRUCTIONS[self.workload]},
                {"role": "user", "content": prompt},
            ],
            "calls": 0,
            "pending": None,
            "answer": None,
            "status": "running",
            "history": [],
        }
        try:
            if self.graph is not None:
                return self.graph.invoke(state, config={"recursion_limit": self.max_calls * 2 + 3})
            while state["status"] == "running":
                state = self.decide(state)
                if state["status"] == "running":
                    state = self.tool(state)
            return state
        finally:
            if self.database is not None:
                self.database.close()
