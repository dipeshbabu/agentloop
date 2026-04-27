from __future__ import annotations

MODEL_PRICES = {
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "default": {"input": 1.00, "output": 3.00},
}


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    price = MODEL_PRICES.get(model or "default", MODEL_PRICES["default"])
    input_cost = (input_tokens / 1000000) * price["input"]
    output_cost = (output_tokens / 1000000) * price["output"]
    return round(input_cost + output_cost, 6)
