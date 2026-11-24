import { writeFile } from "node:fs/promises";
import assert from "node:assert/strict";

const { AgentTrace, AgentLoopClient, AgentLoopClientError } = await import(process.env.AGENTLOOP_SDK_MODULE);
const trace = new AgentTrace("typescript-interop", { metadata: { synthetic: true, task_id: "interop", output: { notes: ["one", "two"] } } });
await trace.tool("lookup", parent => parent.model("format", model => {
  model.setUsage({ inputTokens: 12, outputTokens: 3, provenance: "user_supplied" });
  return { notes: ["one", "two"] };
}, { model: "private-model" }));
trace.finish();
await writeFile(process.argv[2], trace.toJSONString());

const admin = new AgentLoopClient({ baseUrl: process.env.AGENTLOOP_API_URL, adminApiKey: process.env.AGENTLOOP_ADMIN_API_KEY });
assert.equal((await admin.health()).status, "ok");
const credential = await admin.createApiKey("typescript-smoke", "sdk-smoke");
const client = new AgentLoopClient({ baseUrl: process.env.AGENTLOOP_API_URL, apiKey: credential.api_key });
const upload = await client.uploadTrace(trace);
assert.equal(upload.ok, true);
assert.equal(upload.run_id, trace.runId);
const page = await client.listTraces({ pageSize: 1 });
assert.equal(page.traces.length, 1);
assert.equal(page.traces[0].run_id, trace.runId);
const report = await client.getReport(trace.runId);
assert.equal(report.model_call_count, 1);
assert.equal(report.input_tokens, 12);
assert.equal(report.output_tokens, 3);
await client.getDiagnosis(trace.runId);
await client.saveDiagnosis(trace.runId);
assert.ok(Array.isArray((await client.listFindings()).findings));
const quality = await client.buildQualityReport([{ scorer: { type: "required_fields", required: ["notes"] } }], { candidateRunId: trace.runId });
assert.equal(quality.passed, true);
const wrongProject = await admin.createApiKey("other-project", "other");
const other = new AgentLoopClient({ baseUrl: process.env.AGENTLOOP_API_URL, apiKey: wrongProject.api_key });
await assert.rejects(other.getReport(trace.runId), error => error instanceof AgentLoopClientError && error.status === 404);

// Labels, CI runs, deletion, key revocation and the audit log against a real server.
for (const condition of ["baseline", "candidate"]) {
  const labelled = new AgentTrace(`ci-${condition}`, { metadata: { labels: { workflow_run: "ts-1", condition, commit: "abc" } } });
  await labelled.tool("step", () => "done");
  labelled.finish();
  await client.uploadTrace(labelled);
}
const filtered = await client.listTraces({ label: "condition=candidate" });
assert.deepEqual(filtered.traces.map(item => item.name), ["ci-candidate"]);
assert.deepEqual(filtered.traces[0].labels, { workflow_run: "ts-1", condition: "candidate", commit: "abc" });
const runs = await client.listCiRuns();
assert.equal(runs.runs[0].workflow_run, "ts-1");
assert.equal(runs.runs[0].comparable, true);
const comparison = await client.compareCiRun("ts-1");
assert.equal(typeof comparison.markdown, "string");
assert.equal(typeof comparison.replay.gates.passed, "boolean");
const deleted = await client.deleteTrace(trace.runId);
assert.equal(deleted.deleted.traces, 1);
await assert.rejects(client.getReport(trace.runId), error => error instanceof AgentLoopClientError && error.status === 404);
const keys = await admin.listApiKeys("typescript-smoke");
assert.equal(keys.api_keys.length, 1);
assert.ok(!JSON.stringify(keys).includes(credential.api_key));
await admin.revokeApiKey("typescript-smoke", credential.prefix);
await assert.rejects(client.listTraces(), error => error instanceof AgentLoopClientError && error.status === 401);
const audit = await admin.listAuditEvents({ projectId: "typescript-smoke" });
assert.deepEqual(audit.events.map(event => event.action).slice(0, 3), ["api_key.revoked", "trace.deleted", "api_key.created"]);
console.log("TypeScript native trace and authenticated API round trip passed.");
