import { AgentTrace } from "./trace.js";
import type { JsonObject, JsonValue, TraceData } from "./types.js";
import { cloneJson, nonempty } from "./validation.js";

export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
  adminApiKey?: string;
  timeoutMs?: number;
  maxResponseBytes?: number;
  fetch?: typeof globalThis.fetch;
}
export interface RequestOptions { signal?: AbortSignal }
export interface PageOptions { projectId?: string; pageSize?: number; cursor?: string }
export class AgentLoopClientError extends Error {
  readonly status: number | undefined;
  readonly code: "http" | "network" | "timeout" | "aborted" | "response";
  constructor(message: string, code: AgentLoopClientError["code"], status?: number) {
    super(message); this.name = "AgentLoopClientError"; this.code = code; this.status = status;
  }
}

function positive(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${name} must be a positive safe integer`);
  return value;
}
function segment(value: string): string { nonempty(value, "identifier"); return encodeURIComponent(value); }
function query(path: string, values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined) params.set(key, String(value));
  return params.size ? `${path}?${params}` : path;
}

/** Credentials are private and selected by route; requests never follow redirects. */
export class AgentLoopClient {
  readonly #base: string;
  readonly #apiKey?: string;
  readonly #adminKey?: string;
  readonly #timeout: number;
  readonly #maxBytes: number;
  readonly #fetch: typeof globalThis.fetch;
  constructor(options: ClientOptions = {}) {
    let base: URL;
    try { base = new URL(options.baseUrl ?? "http://127.0.0.1:8000"); }
    catch { throw new TypeError("baseUrl must be an absolute HTTP(S) URL"); }
    if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password || base.search || base.hash) {
      throw new TypeError("baseUrl must use HTTP(S) without credentials, query, or fragment");
    }
    base.pathname = base.pathname.replace(/\/+$/, "").replace(/\/v1$/, "");
    this.#base = base.toString().replace(/\/+$/, "");
    for (const [name, key] of [["apiKey", options.apiKey], ["adminApiKey", options.adminApiKey]] as const) {
      if (key !== undefined) { nonempty(key, name); if (/[\r\n]/.test(key)) throw new TypeError(`${name} must not contain newlines`); }
    }
    this.#apiKey = options.apiKey; this.#adminKey = options.adminApiKey;
    this.#timeout = positive(options.timeoutMs ?? 10000, "timeoutMs");
    if (this.#timeout > 2147483647) throw new TypeError("timeoutMs exceeds the supported timer range");
    this.#maxBytes = positive(options.maxResponseBytes ?? 16 * 1024 * 1024, "maxResponseBytes");
    this.#fetch = options.fetch ?? globalThis.fetch;
    if (typeof this.#fetch !== "function") throw new TypeError("A fetch implementation is required");
  }

  async #request(method: string, path: string, payload?: JsonValue, options: RequestOptions = {}, scope: "project" | "admin" = "project"): Promise<JsonObject> {
    const headers = new Headers({ Accept: "application/json" });
    const key = scope === "admin" ? this.#adminKey : this.#apiKey;
    if (key) headers.set(scope === "admin" ? "X-AgentLoop-Admin-Key" : "X-AgentLoop-Key", key);
    const body = payload === undefined ? undefined : JSON.stringify(cloneJson(payload));
    if (body !== undefined) headers.set("Content-Type", "application/json");
    const controller = new AbortController();
    let timedOut = false;
    const abort = () => controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, this.#timeout);
    try {
      const response = await this.#fetch(`${this.#base}/v1${path}`, { method, headers, body, signal: controller.signal, redirect: "error" });
      if (!response.ok) {
        try { await response.body?.cancel(); } catch { /* Preserve the HTTP failure if cancellation fails. */ }
        throw new AgentLoopClientError(`AgentLoop API returned HTTP ${response.status}`, "http", response.status);
      }
      if (response.status === 204) return {};
      const reader = response.body?.getReader();
      const pieces: Uint8Array[] = [];
      let bytes = 0;
      if (reader) {
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            bytes += part.value.byteLength;
            if (bytes > this.#maxBytes) { await reader.cancel(); throw new AgentLoopClientError("AgentLoop response exceeds the configured size limit", "response"); }
            pieces.push(part.value);
          }
        } finally { reader.releaseLock(); }
      }
      const buffer = new Uint8Array(bytes);
      let offset = 0;
      for (const piece of pieces) { buffer.set(piece, offset); offset += piece.byteLength; }
      let result: unknown;
      try { result = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer)); }
      catch { throw new AgentLoopClientError("AgentLoop response is not valid UTF-8 JSON", "response"); }
      if (result === null || typeof result !== "object" || Array.isArray(result)) throw new AgentLoopClientError("AgentLoop response must be a JSON object", "response");
      return result as JsonObject;
    } catch (error) {
      if (error instanceof AgentLoopClientError) throw error;
      if (timedOut) throw new AgentLoopClientError("AgentLoop request timed out", "timeout");
      if (options.signal?.aborted) throw new AgentLoopClientError("AgentLoop request was aborted", "aborted");
      throw new AgentLoopClientError("Could not reach the AgentLoop API", "network");
    } finally { clearTimeout(timer); options.signal?.removeEventListener("abort", abort); }
  }
  health(options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", "/health", undefined, options); }
  ready(options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", "/readyz", undefined, options); }
  createApiKey(projectId = "default", name = "default", options: RequestOptions = {}): Promise<JsonObject> {
    return this.#request("POST", "/api-keys", { project_id: projectId, name }, options, "admin");
  }
  uploadTrace(trace: AgentTrace | TraceData, options: RequestOptions = {}): Promise<JsonObject> {
    const payload = trace instanceof AgentTrace ? trace.toJSON() : trace;
    return this.#request("POST", "/traces", payload as unknown as JsonObject, options);
  }
  listTraces(page: PageOptions = {}, options: RequestOptions = {}): Promise<JsonObject> {
    if (page.pageSize !== undefined) positive(page.pageSize, "pageSize");
    return this.#request("GET", query("/traces", { project_id: page.projectId, page_size: page.pageSize, cursor: page.cursor }), undefined, options);
  }
  usageSummary(projectId?: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", query("/usage", { project_id: projectId }), undefined, options); }
  getReport(runId: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", `/traces/${segment(runId)}/report`, undefined, options); }
  getOptimizationPlan(runId: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", `/traces/${segment(runId)}/optimization`, undefined, options); }
  getDiagnosis(runId: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", `/traces/${segment(runId)}/diagnosis`, undefined, options); }
  saveDiagnosis(runId: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("POST", `/traces/${segment(runId)}/diagnosis`, undefined, options); }
  listFindings(page: PageOptions & { status?: string } = {}, options: RequestOptions = {}): Promise<JsonObject> {
    if (page.pageSize !== undefined) positive(page.pageSize, "pageSize");
    return this.#request("GET", query("/findings", { project_id: page.projectId, status: page.status, page_size: page.pageSize, cursor: page.cursor }), undefined, options);
  }
  updateFindingStatus(runId: string, findingId: string, status: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.#request("POST", `/findings/${segment(runId)}/${segment(findingId)}/status`, { status }, options);
  }
  optimizationQueue(projectId?: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", query("/optimization-queue", { project_id: projectId }), undefined, options); }
  githubIssueDrafts(projectId?: string, limit = 5, options: RequestOptions = {}): Promise<JsonObject> {
    return this.#request("GET", query("/optimization-queue/github-issues", { project_id: projectId, limit: positive(limit, "limit") }), undefined, options);
  }
  buildQualityReport(fixtures: JsonObject[], settings: { baselineRunId?: string; candidateRunId?: string; minScore?: number } = {}, options: RequestOptions = {}): Promise<JsonObject> {
    if (settings.minScore !== undefined && (!Number.isFinite(settings.minScore) || settings.minScore < 0 || settings.minScore > 1)) throw new TypeError("minScore must be between 0 and 1");
    return this.#request("POST", "/quality-reports", { fixtures, baseline_run_id: settings.baselineRunId ?? null, candidate_run_id: settings.candidateRunId ?? null, min_score: settings.minScore ?? null }, options);
  }
  getValueReport(runId: string, settings: { runsPerMonth?: number; engineerHourlyRateUsd?: number; incidentCostUsd?: number } = {}, options: RequestOptions = {}): Promise<JsonObject> {
    return this.#request("GET", query(`/traces/${segment(runId)}/value`, { runs_per_month: settings.runsPerMonth, engineer_hourly_rate_usd: settings.engineerHourlyRateUsd, incident_cost_usd: settings.incidentCostUsd }), undefined, options);
  }
  createIntervention(request: JsonObject, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("POST", "/interventions", request, options); }
  getIntervention(id: string, options: RequestOptions = {}): Promise<JsonObject> { return this.#request("GET", `/interventions/${segment(id)}`, undefined, options); }
}
