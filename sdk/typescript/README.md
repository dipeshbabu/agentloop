# AgentLoop TypeScript SDK

Record model, tool, and retry spans in Node.js 22+ or a modern browser, then export
native AgentLoop traces or send them to the HTTP API. The SDK has no runtime
dependencies. Its API client uses `fetch`; its tracer uses explicit contexts to
keep concurrent work separate.

This SDK is available from the AgentLoop source checkout. It has not been
published to npm. Build and install a local package:

```bash
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
cd sdk/typescript
npm pack
cd ../..
```

From your application directory, install the generated package:

```bash
npm install /path/to/agentloop/sdk/typescript/agentloop-profiler-0.1.0.tgz
```

The package is ESM and includes TypeScript declarations. The SDK's package version
is independent of Python releases; native trace schema 1.1 and API v1 identify
the data contracts it supports.

## Trace your code

```typescript
import { AgentTrace } from "agentloop-profiler";
import { writeFile } from "node:fs/promises";

const trace = new AgentTrace("research_agent", {
  metadata: { task_id: "notes-1", synthetic: true },
});

const notes = await trace.tool("lookup_notes", async () => ["one", "two"]);
trace.setOutput({ notes });
trace.finish();
await writeFile("trace.json", trace.toJSONString());
```

Analyze this local example with the Python CLI:

```bash
agentloop analyze trace.json --html report.html
```

Use a callback's context for nested calls, so their parent IDs are preserved:

```typescript
const modelTrace = new AgentTrace("model_example");
await modelTrace.tool("research", async parent => {
  return await parent.model("summarize", async model => {
    const response = await yourModelCall();
    model.setUsage({
      inputTokens: response.usage.input_tokens,
      outputTokens: response.usage.output_tokens,
      provenance: "provider",
    });
    return response;
  }, { model: "your-model" });
});
modelTrace.finish();
```

`yourModelCall()` belongs to your application. Instrument calls before finishing
the trace. `tool`, `model`, and `retry` callbacks can be synchronous or asynchronous;
each method returns a Promise. Results and thrown business errors propagate
unchanged. `retry` records work you explicitly run and never retries a call itself.

Await all spans before `finish()`. Finishing is idempotent; completed contexts reject
later calls or mutations. `toJSON()` returns an independent native trace object,
and `toJSONString()` returns its JSON representation.

The tracer captures timing and status. It does not read arguments or infer model
usage from arbitrary return values. Missing usage remains `unavailable`; explicit
counts default to `user_supplied` unless you choose another supported provenance.
Word estimates must use `estimated_words`.

Output and input text are explicit: use `trace.setOutput()` for final JSON output,
or a span's `setInput()` / `setOutput()` for text. Raw exception messages are off
by default; `{ captureErrors: true }` enables them. Review retained values before
sharing. `markOutputUnavailable()` prevents quality checks from treating an
intermediate model response as the missing final output.

## Use the API

```typescript
import { AgentLoopClient } from "agentloop-profiler";

const client = new AgentLoopClient({
  baseUrl: "http://127.0.0.1:8000",
  apiKey: "your-project-key",
  timeoutMs: 10_000,
});

await client.uploadTrace(trace);
const report = await client.getReport(trace.runId);
const page = await client.listTraces({ pageSize: 20 });
```

The server must already be running. The SDK does not start it, create an account,
or call a model provider. Use an origin or mounted server URL; a trailing `/v1`
is normalized before requests. IDs and query parameters are encoded.

| Methods | Purpose |
| --- | --- |
| `health`, `ready` | Check server availability and store readiness. |
| `uploadTrace`, `listTraces`, `usageSummary` | Store native traces and inspect project usage. |
| `getReport`, `getOptimizationPlan`, `getValueReport` | Read analysis and estimates. |
| `getDiagnosis`, `saveDiagnosis` | Compute findings or explicitly persist a diagnosis. |
| `listFindings`, `updateFindingStatus`, `optimizationQueue`, `githubIssueDrafts` | Review findings and proposed work. |
| `buildQualityReport` | Evaluate fixtures through the existing API. |
| `createIntervention`, `getIntervention` | Write or read intervention evidence. |
| `createApiKey` | Create a project credential using a separately configured admin key. |

Ordinary requests send only the project key. `createApiKey` sends only the admin
key; neither credential substitutes for the other. Requests reject redirects.
HTTP errors include their status without copying response bodies into error messages.

Pass `{ signal }` to a method to cancel it. `timeoutMs` bounds the request, and
`maxResponseBytes` bounds decoded response data (16 MiB by default). You can supply
a compatible `fetch` implementation in the constructor.

```typescript
import { AgentLoopClientError } from "agentloop-profiler";

try {
  await client.getReport(trace.runId);
} catch (error) {
  if (error instanceof AgentLoopClientError) {
    console.error(error.code, error.status);
  }
}
```

Errors distinguish HTTP, network, timeout, cancellation, and invalid-response
failures. Invalid local arguments raise `TypeError` before sending a request.

See [API v1](https://github.com/dipeshbabu/agentloop/blob/main/docs/API_VERSIONING.md)
and [trace compatibility](https://github.com/dipeshbabu/agentloop/blob/main/docs/TRACE_SCHEMA.md).

## Development

```bash
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm test --prefix sdk/typescript
```

CI checks supported Node versions, the packed installation, Python schema/HTML
interoperability, and a local authenticated API round trip. These checks make no
paid provider calls.

Copyright 2026 Dipesh Tharu Mahato. Licensed under [Apache-2.0](LICENSE).
