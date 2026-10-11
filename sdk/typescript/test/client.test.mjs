import test from "node:test";
import assert from "node:assert/strict";
import { AgentLoopClient, AgentLoopClientError, AgentTrace } from "../dist/index.js";

function capture(options = {}) {
  const requests = [];
  const client = new AgentLoopClient({ ...options, fetch: async (url, init) => {
    requests.push({ url, ...init }); return new Response(JSON.stringify({ ok: true }), { status: 200 });
  } });
  return { client, requests };
}

test("API v1 prefixes preserve mount paths and normalize a supplied v1 suffix", async () => {
  for (const baseUrl of ["https://example.test/mount", "https://example.test/mount/", "https://example.test/mount/v1/"]) {
    const { client, requests } = capture({ baseUrl }); await client.getReport("team/run ?1");
    assert.equal(requests[0].url, "https://example.test/mount/v1/traces/team%2Frun%20%3F1/report");
    assert.equal(requests[0].redirect, "error");
  }
});

test("admin credentials never accompany ordinary requests or substitute for project credentials", async () => {
  const { client, requests } = capture({ apiKey: "project-key", adminApiKey: "admin-key" });
  await client.health(); await client.listTraces(); await client.getDiagnosis("run");
  await client.createApiKey("project", "key");
  for (const request of requests.slice(0, -1)) {
    assert.equal(request.headers.get("X-AgentLoop-Key"), "project-key");
    assert.equal(request.headers.get("X-AgentLoop-Admin-Key"), null);
  }
  const admin = requests.at(-1);
  assert.equal(admin.headers.get("X-AgentLoop-Admin-Key"), "admin-key");
  assert.equal(admin.headers.get("X-AgentLoop-Key"), null);
  const noProject = capture({ adminApiKey: "admin-key" }); await noProject.client.listTraces();
  assert.equal(noProject.requests[0].headers.get("X-AgentLoop-Admin-Key"), null);
  assert.equal(noProject.requests[0].headers.get("X-AgentLoop-Key"), null);
  assert.ok(!JSON.stringify(client).includes("key"));
});

test("pagination, status updates, quality, and intervention requests match the Python API", async () => {
  const { client, requests } = capture();
  await client.listTraces({ projectId: "a b", pageSize: 2, cursor: "x+y" });
  await client.updateFindingStatus("run/a", "finding/b", "accepted");
  await client.buildQualityReport([{ expected: "ok" }], { candidateRunId: "run", minScore: 0.9 });
  await client.createIntervention({ task_id: "task" }); await client.getIntervention("int/a");
  assert.ok(requests[0].url.endsWith("project_id=a+b&page_size=2&cursor=x%2By"));
  assert.ok(requests[1].url.endsWith("/findings/run%2Fa/finding%2Fb/status"));
  assert.deepEqual(JSON.parse(requests[1].body), { status: "accepted" });
  assert.deepEqual(JSON.parse(requests[2].body), { fixtures: [{ expected: "ok" }], baseline_run_id: null, candidate_run_id: "run", min_score: 0.9 });
  assert.ok(requests[4].url.endsWith("/interventions/int%2Fa"));
});

test("upload preserves native completed traces without sending credentials in the body", async () => {
  const trace = new AgentTrace("upload"); await trace.tool("work", () => "done"); trace.finish();
  const { client, requests } = capture({ apiKey: "PRIVATE_KEY" }); await client.uploadTrace(trace);
  assert.deepEqual(JSON.parse(requests[0].body), trace.toJSON());
  assert.ok(!requests[0].body.includes("PRIVATE_KEY"));
  const pending = new AgentTrace("pending");
  assert.throws(() => client.uploadTrace(pending), /Finish/);
});

test("HTTP and invalid-response errors expose status without response or credential contents", async () => {
  const http = new AgentLoopClient({ apiKey: "PRIVATE_KEY", fetch: async () => new Response("PRIVATE_RESPONSE", { status: 401 }) });
  await assert.rejects(http.health(), error => error instanceof AgentLoopClientError && error.status === 401 && !String(error).includes("PRIVATE_"));
  for (const body of ["not json", "[]", "null"]) {
    const client = new AgentLoopClient({ fetch: async () => new Response(body) });
    await assert.rejects(client.health(), error => error.code === "response");
  }
});

test("response size limits stop streaming payloads", async () => {
  let cancelled = false;
  const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode("x".repeat(20))); }, cancel() { cancelled = true; } });
  const client = new AgentLoopClient({ maxResponseBytes: 8, fetch: async () => new Response(body) });
  await assert.rejects(client.health(), error => error.code === "response");
  assert.equal(cancelled, true);
});

test("timeouts and caller cancellation abort the underlying request", async () => {
  const fetch = (_, options) => new Promise((_, reject) => {
    if (options.signal.aborted) reject(new Error("PRIVATE_REASON"));
    else options.signal.addEventListener("abort", () => reject(new Error("PRIVATE_REASON")), { once: true });
  });
  const timeout = new AgentLoopClient({ timeoutMs: 5, fetch });
  await assert.rejects(timeout.health(), error => error.code === "timeout" && !error.message.includes("PRIVATE"));
  const controller = new AbortController(); const client = new AgentLoopClient({ fetch });
  const request = client.health({ signal: controller.signal }); controller.abort("PRIVATE_REASON");
  await assert.rejects(request, error => error.code === "aborted");
});

test("invalid URLs, credentials, and options are rejected before requests", () => {
  for (const baseUrl of ["file:///tmp/x", "https://user:PRIVATE_KEY@example.test", "https://example.test?x=1", "https://example.test/#x", "invalid"]) {
    assert.throws(() => new AgentLoopClient({ baseUrl }), error => !String(error).includes("PRIVATE_KEY"));
  }
  for (const timeoutMs of [0, -1, Infinity, NaN, 2147483648]) assert.throws(() => new AgentLoopClient({ timeoutMs }));
  assert.throws(() => new AgentLoopClient({ apiKey: "x\ny" }));
});
