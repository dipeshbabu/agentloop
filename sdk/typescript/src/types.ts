export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject { [key: string]: JsonValue }
export type TokenProvenance = "provider" | "tokenizer" | "user_supplied" | "estimated_words" | "unavailable" | "unspecified" | "external_reported";
export interface AgentEvent {
  event_id: string;
  run_id: string;
  event_type: "model_call" | "tool_call" | "retry";
  name: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  parent_id: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  token_provenance: TokenProvenance;
  input_text: string | null;
  output_text: string | null;
  status: "ok" | "error";
  error: string | null;
  metadata: JsonObject;
}
export interface TraceData {
  schema_version: "1.1";
  name: string;
  run_id: string;
  started_at: string;
  ended_at: string;
  elapsed_ms: number;
  metadata: JsonObject;
  events: AgentEvent[];
}
export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  provenance?: TokenProvenance;
}
export interface SpanOptions {
  model?: string;
  usage?: TokenUsage;
  metadata?: JsonObject;
}
export interface TraceOptions {
  runId?: string;
  metadata?: JsonObject;
  captureErrors?: boolean;
}
