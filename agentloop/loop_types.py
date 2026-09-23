"""Explicit, payload-free evidence for operational retry and loop guards."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def fingerprint(value: Any) -> str:
    """Hash selected finite JSON values; never serialize arbitrary object reprs.

    Mapping order is ignored. Represent meaningful order as a list. Inputs are
    not retained, but hashes of predictable or sensitive values are not anonymous.
    """

    def validate(item: Any) -> None:
        if item is None or type(item) in {str, bool, int}:
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("fingerprint inputs must be finite JSON values with string keys")

    try:
        validate(value)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except RecursionError:
        raise ValueError("fingerprint inputs must not contain cycles") from None
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StepInfo:
    """Caller-declared step identity, state evidence, polling, and retry safety."""

    step_id: str
    argument_fingerprint: str | None = None
    progress_fingerprint: str | None = None
    polling: bool = False
    mutating: bool | None = None
    retry_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id:
            raise ValueError("step_id must be a nonempty opaque identifier")
        for name in ("argument_fingerprint", "progress_fingerprint"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
                raise ValueError("step fingerprints must be SHA-256 hex digests")
        if type(self.polling) is not bool or type(self.retry_safe) is not bool:
            raise ValueError("polling and retry_safe must be booleans")
        if self.mutating is not None and type(self.mutating) is not bool:
            raise ValueError("mutating must be a boolean or unknown")
