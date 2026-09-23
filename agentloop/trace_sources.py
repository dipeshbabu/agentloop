"""Native source-trace verification across compatible numeric normalization."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from agentloop.interventions import canonical_json, trace_fingerprint
from agentloop.tracer import AgentTrace


@dataclass(frozen=True)
class TraceSource:
    trace: AgentTrace
    fingerprints: frozenset[str]

    @classmethod
    def from_dict(cls, payload):
        """Validate before accepting either canonical source or normalized identity.

        Ledger creation can precede export (integer duration fields) or follow a
        store read (normalized floats/defaults). Preserve both existing identities;
        do not alter historical ledger hashes or recompute reports/pricing.
        """
        trace = AgentTrace.from_dict(payload)
        return cls(
            trace,
            frozenset(
                {sha256(canonical_json(payload).encode()).hexdigest(), trace_fingerprint(trace)}
            ),
        )
