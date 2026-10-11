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
console.log("TypeScript native trace and authenticated API round trip passed.");
