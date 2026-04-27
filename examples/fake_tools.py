from __future__ import annotations

import time


def sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000)


def fake_search(query: str) -> list[str]:
    sleep_ms(300)
    return [f"source-{i}-{query}" for i in range(1, 6)]


def fake_read(source: str) -> str:
    sleep_ms(250)
    return f"Content from {source}. " * 30
