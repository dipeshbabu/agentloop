import { AgentTrace, AgentLoopClient, type TraceData, type JsonObject } from "../dist/index.js";

const trace = new AgentTrace("typed");
const value: number = await trace.model("model", async context => {
  context.setUsage({ inputTokens: 1, outputTokens: 2, provenance: "provider" });
  return 5;
});
trace.setOutput({ value });
const native: TraceData = trace.finish().toJSON();
const client = new AgentLoopClient();
const result: Promise<JsonObject> = client.uploadTrace(native);
void result;
// @ts-expect-error usage counts are numeric
trace.model("bad", () => 0, { usage: { inputTokens: "one", outputTokens: 2 } });
// @ts-expect-error provenance has a bounded vocabulary
trace.model("bad", () => 0, { usage: { inputTokens: 1, outputTokens: 2, provenance: "exact" } });
// @ts-expect-error native schema is explicitly versioned
const invalid: TraceData = { ...native, schema_version: "2.0" };
void invalid;
