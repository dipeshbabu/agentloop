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
  function visit(item: unknown): JsonValue {
    if (item === null || typeof item === "string" || typeof item === "boolean") return item;
    if (typeof item === "number") {
      if (!Number.isFinite(item)) throw new TypeError("JSON numbers must be finite");
      return item;
    }
    if (typeof item !== "object" || !item) throw new TypeError("Value must contain only JSON data");
    if (ancestors.has(item)) throw new TypeError("JSON data must not contain cycles");
    if (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null) {
      throw new TypeError("JSON objects must be plain objects");
    }
    ancestors.add(item);
    let copy: JsonValue;
    if (Array.isArray(item)) {
      const array: JsonValue[] = [];
      const length = item.length;
      for (let index = 0; index < length; index++) {
        if (!Object.hasOwn(item, index)) throw new TypeError("JSON arrays must not contain empty slots");
        array.push(visit(item[index]));
      }
      copy = array;
    } else {
      const object: JsonObject = {};
      for (const key of Object.keys(item)) {
        const entry = visit((item as Record<string, unknown>)[key]);
        if (key === "__proto__") Object.defineProperty(object, key, { value: entry, enumerable: true, writable: true, configurable: true });
        else object[key] = entry;
      }
      copy = object;
    }
    ancestors.delete(item);
    return copy;
  }
  return visit(value) as T;
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
