"""Explicit investigation declarations for optional semantic waste findings."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from agentloop.judgment_types import canonical, finite, text

SEMANTIC_WASTE_KEY = "agentloop.semantic_waste"
SEMANTIC_WASTE_VERSION = "1.0"
QUESTIONS = {
    "semantic_redundancy": "Do the target steps materially repeat work already available in the reference steps, without a required independent check?",
    "low_contribution": "Do the target steps add no material information needed by the supplied downstream result under the task criteria?",
    "semantic_no_progress": "Does the target sequence fail to make meaningful progress under the task criteria, rather than perform required polling, verification or state transitions?",
    "retry_usefulness": "Do the target retries repeat the same failure or input without useful new evidence, rather than satisfy an intentional retry policy?",
    "context_relevance": "Is the supplied context in the target steps unrelated to the decision under the task criteria?",
}


@dataclass(frozen=True)
class SemanticInvestigation:
    investigation_id: str
    family: str
    target_spans: tuple[str, ...]
    reference_spans: tuple[str, ...]
    task_criteria: str
    criteria_ref: str
    summaries: tuple[tuple[str, str], ...] = ()
    retention_required: bool = False
    reuse_safe: bool = False
    reuse_contract_ref: str | None = None
    removal_attribution_ref: str | None = None
    minimum_confidence: float = 0.8

    def __post_init__(self):
        for key in ("investigation_id", "task_criteria", "criteria_ref"):
            text(getattr(self, key), key)
        if len(self.task_criteria) > 2048:
            raise ValueError("task_criteria must be at most 2048 characters")
        if not finite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be in [0, 1]")
        if self.family not in QUESTIONS:
            raise ValueError("unsupported semantic finding family")
        for key in ("target_spans", "reference_spans"):
            value = getattr(self, key)
            if not isinstance(value, (tuple, list)) or not value:
                raise ValueError(f"{key} requires at least one span ID")
            for item in value:
                text(item, key)
            if len(set(value)) != len(value):
                raise ValueError(f"{key} must be unique")
            object.__setattr__(self, key, tuple(value))
        if set(self.target_spans) & set(self.reference_spans):
            raise ValueError("target and reference spans must be distinct")
        if len(self.evidence_spans) > 64:
            raise ValueError("an investigation can reference at most 64 spans")
        if self.family == "semantic_no_progress" and len(self.target_spans) < 2:
            raise ValueError("no-progress investigation requires a sequence of target spans")
        for key in ("retention_required", "reuse_safe"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be a boolean declaration")
        for key in ("reuse_contract_ref", "removal_attribution_ref"):
            text(getattr(self, key), key, optional=True)
        if self.reuse_safe and self.reuse_contract_ref is None:
            raise ValueError("safe reuse requires a supporting contract reference")
        values = self.summaries.items() if isinstance(self.summaries, dict) else self.summaries
        try:
            pairs = tuple((key, value) for key, value in values)
        except (TypeError, ValueError):
            raise ValueError("summaries must map span IDs to approved derived text") from None
        for key, value in pairs:
            text(key, "summary span ID")
            text(value, "summary")
        if len(dict(pairs)) != len(pairs) or set(dict(pairs)) - set(self.evidence_spans):
            raise ValueError("summaries must name unique selected spans")
        object.__setattr__(self, "summaries", tuple(sorted(pairs)))

    @property
    def evidence_spans(self):
        return self.reference_spans + self.target_spans

    def to_dict(self):
        return json.loads(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value):
        return cls(**value)
