import test from "node:test";
import assert from "node:assert/strict";
import { AgentTrace } from "../dist/index.js";

test("nested spans preserve outputs, parentage, measured runtime, and usage", async () => {
  const trace = new AgentTrace("agent", { metadata: { synthetic: true } });
  const output = { notes: ["local"] };
  const result = await trace.tool("lookup", async span => {
    return await span.model("answer", async model => {
      model.setUsage({ inputTokens: 10, outputTokens: 2, provenance: "provider" });
      await new Promise(resolve => setTimeout(resolve, 5));
      return output;
    }, { model: "test-model" });
  });
  assert.equal(result, output);
  trace.setOutput(output); trace.finish();
  const data = trace.toJSON();
  const model = data.events.find(event => event.event_type === "model_call");
  const tool = data.events.find(event => event.event_type === "tool_call");
  assert.equal(model.parent_id, tool.event_id);
  assert.equal(model.run_id, data.run_id);
  assert.equal(model.input_tokens, 10);
  assert.equal(model.output_tokens, 2);
  assert.equal(model.token_provenance, "provider");
  assert.ok(data.elapsed_ms >= model.duration_ms);
  assert.deepEqual(data.metadata.output, output);
  assert.deepEqual(JSON.parse(trace.toJSONString()), data);
});

test("concurrent traces and sibling spans never share ownership", async () => {
  const traces = await Promise.all(Array.from({ length: 8 }, async (_, index) => {
    const trace = new AgentTrace(`agent-${index}`);
    await trace.tool("parent", async parent => {
      await Promise.all([parent.tool("one", async () => index), parent.model("two", async () => index)]);
    });
    return trace.finish().toJSON();
  }));
  assert.equal(new Set(traces.map(trace => trace.run_id)).size, 8);
  for (const trace of traces) {
    const parent = trace.events.find(event => event.name === "parent");
    assert.ok(trace.events.every(event => event.run_id === trace.run_id));
    assert.ok(trace.events.filter(event => event !== parent).every(event => event.parent_id === parent.event_id));
  }
});

test("business exceptions propagate unchanged while recording failure without message capture", async () => {
  const trace = new AgentTrace("failure");
  const failure = new Error("PRIVATE_ERROR");
  await assert.rejects(trace.model("call", async () => { throw failure; }), error => error === failure);
  const data = trace.finish().toJSON();
  assert.equal(data.events[0].status, "error");
  assert.equal(data.events[0].error, null);
  assert.ok(!trace.toJSONString().includes("PRIVATE_ERROR"));
});

test("raw text and errors require explicit capture", async () => {
  const trace = new AgentTrace("capture", { captureErrors: true });
  await assert.rejects(trace.tool("call", span => {
    span.setInput("input"); span.setOutput("output"); throw new Error("captured");
  }));
  const data = trace.finish().toJSON();
  assert.equal(data.events[0].input_text, "input");
  assert.equal(data.events[0].output_text, "output");
  assert.equal(data.events[0].error, "captured");
});

test("finish waits for active spans, is idempotent, and prohibits later mutation", async () => {
  const trace = new AgentTrace("pending");
  let resolve;
  let context;
  const pending = trace.tool("wait", span => { context = span; return new Promise(done => { resolve = done; }); });
  assert.throws(() => trace.finish(), /active spans/);
  assert.throws(() => trace.toJSON(), /Finish/);
  resolve("done"); await pending;
  trace.finish(); const first = trace.toJSON();
  trace.finish(); assert.deepEqual(trace.toJSON(), first);
  await assert.rejects(trace.tool("late", () => "ignored"), /closed/);
  assert.throws(() => context.setOutput("late"), /closed/);
  assert.throws(() => trace.setOutput("late"), /finished/);
});

test("JSON metadata is copied and rejects cycles and unsupported values", () => {
  const original = { task: { value: 1 } };
  const trace = new AgentTrace("metadata", { metadata: original });
  original.task.value = 2; trace.finish();
  const data = trace.toJSON(); data.metadata.task.value = 3;
  assert.equal(trace.toJSON().metadata.task.value, 1);
  for (const value of [NaN, Infinity, undefined, 1n, () => {}, new Date()]) {
    assert.throws(() => new AgentTrace("invalid", { metadata: { value } }));
  }
  const cycle = {}; cycle.self = cycle;
  assert.throws(() => new AgentTrace("cyclic", { metadata: cycle }), /cycles/);
});

test("missing usage stays unavailable and invalid counts fail before calling a provider", async () => {
  const trace = new AgentTrace("usage");
  await trace.model("missing", () => "ok");
  let called = false;
  for (const inputTokens of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, true]) {
    await assert.rejects(trace.model("invalid", () => { called = true; }, { usage: { inputTokens, outputTokens: 0 } }));
  }
  assert.equal(called, false);
  const event = trace.finish().toJSON().events[0];
  assert.equal(event.token_provenance, "unavailable");
  assert.equal(event.input_tokens, 0);
});

test("retried work is recorded only when the caller explicitly runs it", async () => {
  const trace = new AgentTrace("retry");
  let calls = 0;
  assert.equal(await trace.retry("retry", () => { calls++; return 3; }), 3);
  assert.equal(calls, 1);
  assert.equal(trace.finish().toJSON().events[0].event_type, "retry");
});

test("runtime metadata and privacy options cannot create invalid native traces", () => {
  for (const metadata of [[], "text", 5]) assert.throws(() => new AgentTrace("invalid", { metadata }));
  assert.throws(() => new AgentTrace("invalid", { captureErrors: "false" }));
  const sparse = []; sparse.length = 1;
  assert.throws(() => new AgentTrace("invalid", { metadata: { sparse } }), /empty slots/);
  const trace = new AgentTrace("immutable");
  assert.throws(() => { trace.runId = "changed"; });
});

test("explicit final output restores quality evidence after an unavailable marker", () => {
  const trace = new AgentTrace("output");
  trace.markOutputUnavailable(); trace.setOutput({ answer: "ok" });
  const data = trace.finish().toJSON();
  assert.equal(data.metadata.output_unavailable, undefined);
  assert.deepEqual(data.metadata.output, { answer: "ok" });
});
