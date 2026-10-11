import type { JsonObject, JsonValue, TokenProvenance, TokenUsage } from "./types.js";

export function nonempty(value: unknown, name: string): asserts value is string {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${name} must be a nonempty string`);
}

export function count(value: unknown, name: string): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a nonnegative safe integer`);
  }
}

export function cloneJson<T>(value: T): T {
  const ancestors = new Set<object>();
  function visit(item: unknown): void {
    if (item === null || typeof item === "string" || typeof item === "boolean") return;
    if (typeof item === "number") {
      if (!Number.isFinite(item)) throw new TypeError("JSON numbers must be finite");
      return;
    }
    if (typeof item !== "object" || !item) throw new TypeError("Value must contain only JSON data");
    if (ancestors.has(item)) throw new TypeError("JSON data must not contain cycles");
    if (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null) {
      throw new TypeError("JSON objects must be plain objects");
    }
    ancestors.add(item);
    if (Array.isArray(item) && Array.from({ length: item.length }, (_, index) => index).some(index => !(index in item))) {
      throw new TypeError("JSON arrays must not contain empty slots");
    }
    for (const entry of Object.values(item)) visit(entry);
    ancestors.delete(item);
  }
  visit(value);
  return JSON.parse(JSON.stringify(value)) as T;
}

const bases = new Set<TokenProvenance>(["provider", "tokenizer", "user_supplied", "estimated_words", "unavailable", "unspecified", "external_reported"]);
export function usage(value: TokenUsage): Required<TokenUsage> {
  count(value.inputTokens, "inputTokens");
  count(value.outputTokens, "outputTokens");
  const provenance = value.provenance ?? "user_supplied";
  if (!bases.has(provenance)) throw new TypeError("Unsupported token provenance");
  if (provenance === "unavailable" && (value.inputTokens !== 0 || value.outputTokens !== 0)) {
    throw new TypeError("Unavailable usage cannot have positive token counts");
  }
  return { inputTokens: value.inputTokens, outputTokens: value.outputTokens, provenance };
}

export function asJson(value: unknown): JsonValue { return cloneJson(value) as JsonValue; }

export function cloneObject(value: JsonObject): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("Metadata must be a JSON object");
  return cloneJson(value);
}
