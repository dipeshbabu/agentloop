import { AgentTrace, AgentLoopClient } from "agentloop-profiler";
import { AgentLoopClient as ClientEntry } from "agentloop-profiler/client";

function check(condition, message) {
  if (!condition) throw new Error(message);
}

export async function exercise() {
  check(ClientEntry === AgentLoopClient, "Public client entry points differ");
  const trace = new AgentTrace("browser-research", {
    metadata: { synthetic: true, source: "installed-browser-smoke" },
  });
  const answer = await trace.tool("lookup", async parent => {
    const parts = await Promise.all(["alpha", "beta"].map(name =>
      parent.model(name, span => {
        span.setUsage({ inputTokens: 3, outputTokens: 1, provenance: "user_supplied" });
        return name;
      }),
    ));
    return parts.join(" ");
  });
  const failure = new Error("PRIVATE_BROWSER_ERROR");
  try {
    await trace.model("failed", () => { throw failure; });
    throw new Error("Business failure was swallowed");
  } catch (error) {
    check(error === failure, "Business error identity changed");
  }
  trace.setOutput({ answer });
  trace.finish();
  const data = trace.toJSON();
  check(data.schema_version === "1.1", "Wrong native schema");
  check(data.events.length === 4, "Lost browser spans");
  const parent = data.events.find(event => event.name === "lookup");
  const nested = data.events.filter(event => ["alpha", "beta"].includes(event.name));
  check(nested.length === 2 && nested.every(event => event.parent_id === parent.event_id),
    "Concurrent parent relationships changed");
  const failed = data.events.find(event => event.name === "failed");
  check(failed.status === "error" && failed.token_provenance === "unavailable",
    "Failed attempts or unknown usage were lost");
  check(!trace.toJSONString().includes("PRIVATE_BROWSER_ERROR"), "Error content leaked");

  const client = new ClientEntry({ baseUrl: location.origin, apiKey: "browser-test-project" });
  const health = await client.health();
  check(health.status === "ok", "Browser fetch failed");
  const uploaded = await client.uploadTrace(trace);
  check(uploaded.run_id === trace.runId, "Browser upload changed trace identity");
  const cancelled = new AbortController();
  cancelled.abort();
  try {
    await client.getReport(trace.runId, { signal: cancelled.signal });
    throw new Error("Cancelled request was accepted");
  } catch (error) {
    check(error.code === "aborted", "Cancellation classification changed");
  }
  try {
    await client.getReport("redirect");
    throw new Error("Redirect was followed");
  } catch (error) {
    check(error.code === "network", "Redirect rejection changed");
  }
  return { trace: data, answer, checks: 11 };
}

globalThis.smokeResult = exercise().then(
  result => ({ passed: true, ...result }),
  error => ({ passed: false, error: String(error) }),
);
