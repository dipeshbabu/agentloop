import type { AgentEvent, JsonObject, JsonValue, SpanOptions, TokenUsage, TraceData, TraceOptions } from "./types.js";
import { cloneJson, cloneObject, nonempty, usage } from "./validation.js";

export class SpanContext {
  readonly #trace: AgentTrace;
  readonly #event: AgentEvent | null;
  #open = true;
  constructor(trace: AgentTrace, event: AgentEvent | null) { this.#trace = trace; this.#event = event; }
  get eventId(): string | null { return this.#event?.event_id ?? null; }
  close(): void { this.#open = false; }
  #assertOpen(): void { if (!this.#open) throw new Error("Span context is already closed"); }
  setUsage(value: TokenUsage): void {
    this.#assertOpen();
    if (this.#event?.event_type !== "model_call") throw new Error("Usage belongs to a model span");
    const resolved = usage(value);
    this.#event.input_tokens = resolved.inputTokens;
    this.#event.output_tokens = resolved.outputTokens;
    this.#event.token_provenance = resolved.provenance;
  }
  setInput(text: string): void { this.#assertOpen(); if (typeof text !== "string") throw new TypeError("Input text must be a string"); if (!this.#event) throw new Error("Input belongs to a span"); this.#event.input_text = text; }
  setOutput(text: string): void { this.#assertOpen(); if (typeof text !== "string") throw new TypeError("Output text must be a string"); if (!this.#event) throw new Error("Output belongs to a span"); this.#event.output_text = text; }
  async tool<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> {
    this.#assertOpen(); return this.#trace.runSpan("tool_call", name, body, options, this.eventId);
  }
  async model<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> {
    this.#assertOpen(); return this.#trace.runSpan("model_call", name, body, options, this.eventId);
  }
  async retry<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> {
    this.#assertOpen(); return this.#trace.runSpan("retry", name, body, options, this.eventId);
  }
}

/** Explicit contexts keep concurrent tasks isolated in Node and modern browsers. */
export class AgentTrace {
  readonly #name: string;
  readonly #runId: string;
  readonly #startedAt = new Date().toISOString();
  readonly #started = performance.now();
  readonly #events: AgentEvent[] = [];
  readonly #activeIds = new Set<string>();
  readonly #captureErrors: boolean;
  readonly #root: SpanContext;
  #metadata: JsonObject;
  #active = 0;
  #endedAt: string | null = null;
  #elapsed = 0;

  constructor(name: string, options: TraceOptions = {}) {
    nonempty(name, "name");
    const runId = options.runId ?? `run_${crypto.randomUUID().replaceAll("-", "")}`;
    nonempty(runId, "runId");
    this.#name = name;
    this.#runId = runId;
    this.#metadata = cloneObject(options.metadata ?? {});
    if (options.captureErrors !== undefined && typeof options.captureErrors !== "boolean") throw new TypeError("captureErrors must be boolean");
    this.#captureErrors = options.captureErrors ?? false;
    this.#root = new SpanContext(this, null);
  }
  get name(): string { return this.#name; }
  get runId(): string { return this.#runId; }
  setMetadata(key: string, value: JsonValue): void {
    this.#assertOpen(); nonempty(key, "metadata key");
    Object.defineProperty(this.#metadata, key, { value: cloneJson(value), writable: true, enumerable: true, configurable: true });
  }
  setOutput(value: JsonValue): void { this.setMetadata("output", value); delete this.#metadata.output_unavailable; }
  markOutputUnavailable(): void { this.setMetadata("output_unavailable", true); }
  #assertOpen(): void { if (this.#endedAt !== null) throw new Error("Trace is already finished"); }
  tool<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> { return this.#root.tool(name, body, options); }
  model<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> { return this.#root.model(name, body, options); }
  retry<T>(name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions = {}): Promise<T> { return this.#root.retry(name, body, options); }

  async runSpan<T>(kind: AgentEvent["event_type"], name: string, body: (span: SpanContext) => T | Promise<T>, options: SpanOptions, parentId: string | null): Promise<T> {
    this.#assertOpen(); nonempty(name, "span name");
    if (parentId !== null && !this.#activeIds.has(parentId)) throw new Error("Parent span must be active in this trace");
    if (typeof body !== "function") throw new TypeError("Span body must be callable");
    if (options.model !== undefined) nonempty(options.model, "model");
    const resolved = options.usage ? usage(options.usage) : { inputTokens: 0, outputTokens: 0, provenance: "unavailable" as const };
    if (kind !== "model_call" && options.usage) throw new TypeError("Usage belongs to a model span");
    const event: AgentEvent = {
      event_id: `evt_${crypto.randomUUID().replaceAll("-", "")}`, run_id: this.runId,
      event_type: kind, name, started_at: new Date().toISOString(), ended_at: "", duration_ms: 0,
      parent_id: parentId, model: options.model ?? null, input_tokens: resolved.inputTokens,
      output_tokens: resolved.outputTokens, token_provenance: resolved.provenance,
      input_text: null, output_text: null, status: "ok", error: null, metadata: cloneObject(options.metadata ?? {}),
    };
    const started = performance.now();
    const span = new SpanContext(this, event);
    this.#active++;
    this.#activeIds.add(event.event_id);
    try { return await body(span); }
    catch (error) {
      event.status = "error";
      if (this.#captureErrors) {
        try { event.error = error instanceof Error ? error.message : String(error); } catch { event.error = null; }
      }
      throw error;
    } finally {
      event.ended_at = new Date().toISOString();
      event.duration_ms = Math.max(0, performance.now() - started);
      span.close(); this.#events.push(event); this.#active--; this.#activeIds.delete(event.event_id);
    }
  }
  finish(): this {
    if (this.#endedAt !== null) return this;
    if (this.#active) throw new Error("Await all active spans before finishing the trace");
    this.#endedAt = new Date().toISOString();
    this.#elapsed = Math.max(0, performance.now() - this.#started);
    this.#root.close(); return this;
  }
  toJSON(): TraceData {
    if (this.#endedAt === null) throw new Error("Finish the trace before exporting it");
    return cloneJson({ schema_version: "1.1", name: this.name, run_id: this.runId, started_at: this.#startedAt,
      ended_at: this.#endedAt, elapsed_ms: this.#elapsed, metadata: this.#metadata, events: this.#events });
  }
  toJSONString(): string { return JSON.stringify(this.toJSON(), null, 2); }
}

export function traceAgent(name: string, options: TraceOptions = {}): AgentTrace { return new AgentTrace(name, options); }
