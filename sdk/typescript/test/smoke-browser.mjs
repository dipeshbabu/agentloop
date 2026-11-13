/** Test a packed SDK in a fresh esbuild application and isolated Chromium. */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { chromium } from "playwright";

const sdk = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(join(tmpdir(), "agentloop-browser-"));
const application = join(temporary, "application");
await mkdir(application);
// npm_execpath selects the running npm CLI without a shell on Windows.
assert.ok(process.env.npm_execpath, "Run this smoke with npm run test:browser");
function npm(args, cwd) {
  return execFileSync(process.execPath, [process.env.npm_execpath, ...args], {
    cwd, encoding: "utf8", timeout: 120_000,
  });
}
const packed = JSON.parse(npm(["pack", "--json", "--pack-destination", temporary], sdk));
assert.equal(packed.length, 1);
const tarball = join(temporary, packed[0].filename);
npm(["install", "--ignore-scripts", "--no-audit", "--no-fund", tarball], application);
const source = await readFile(join(sdk, "test/browser-app.mjs"), "utf8");
const entry = join(application, "app.mjs");
await writeFile(entry, source);
const bundled = await build({
  entryPoints: [entry], bundle: true, platform: "browser", format: "esm",
  target: "es2022", write: false, metafile: true,
});
assert.ok(Object.keys(bundled.metafile.inputs).some(path => path.includes("node_modules/agentloop-profiler/")));
const bundle = bundled.outputFiles[0].contents;
const requests = [];
let uploaded;
const server = createServer(async (request, response) => {
  try {
    requests.push({ path: request.url, project: request.headers["x-agentloop-key"],
      admin: request.headers["x-agentloop-admin-key"] });
    if (request.url === "/") {
      response.setHeader("Content-Type", "text/html");
      response.end('<!doctype html><title>SDK smoke</title><script type="module" src="/app.js"></script>');
    } else if (request.url === "/app.js") {
      response.setHeader("Content-Type", "text/javascript");
      response.end(bundle);
    } else if (request.url === "/v1/health") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ status: "ok" }));
    } else if (request.url === "/v1/traces" && request.method === "POST") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      uploaded = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ run_id: uploaded.run_id }));
    } else if (request.url === "/v1/traces/redirect/report") {
      response.writeHead(302, { Location: "/redirect-target" });
      response.end();
    } else {
      response.writeHead(404);
      response.end();
    }
  } catch {
    response.writeHead(500);
    response.end();
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  browser = await chromium.launch(process.env.AGENTLOOP_BROWSER_CHANNEL
    ? { channel: process.env.AGENTLOOP_BROWSER_CHANNEL } : {});
  const context = await browser.newContext({ serviceWorkers: "block" });
  const origin = `http://127.0.0.1:${server.address().port}`;
  await context.route("**/*", route => {
    if (new URL(route.request().url()).origin === origin) return route.continue();
    return route.abort();
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(origin);
  await page.waitForFunction(() => globalThis.smokeResult !== undefined);
  const result = await page.evaluate(() => globalThis.smokeResult);
  assert.deepEqual(errors, []);
  assert.equal(result.passed, true, result.error);
  assert.equal(result.answer, "alpha beta");
  assert.deepEqual(uploaded, result.trace);
  const apiRequests = requests.filter(request => request.path.startsWith("/v1/"));
  assert.ok(apiRequests.length >= 3);
  assert.ok(apiRequests.every(request => request.project === "browser-test-project" && !request.admin));
  assert.ok(!requests.some(request => request.path === "/redirect-target"));
  const out = process.argv[2] ? resolve(process.argv[2]) : join(temporary, "evidence");
  await mkdir(out);
  await writeFile(join(out, "trace.json"), JSON.stringify(result.trace, null, 2) + "\n");
  const summary = { passed: true, browser: browser.version(), node: process.version,
    sdkVersion: packed[0].version, tarballIntegrity: packed[0].integrity,
    bundler: "esbuild", checks: result.checks, requestCount: apiRequests.length,
    scope: "Owned offline fixture in Chromium and esbuild; same-origin fetch; no external users or model providers" };
  await writeFile(join(out, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
  console.log(JSON.stringify({ ...summary, out }));
  await context.close();
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
