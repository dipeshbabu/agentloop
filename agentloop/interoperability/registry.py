"""Versioned capability declarations and source/version-scoped probe evidence.

Catalog JSON is data. Enumeration does not load entry points, optional SDKs or
vendor runtimes, and registry state never changes an existing harness policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from agentloop.interoperability.validation import (
    ImportValidationError,
    load_json_artifact,
    relative_reference,
    validate_json_tree,
)
from agentloop.interventions import canonical_json

CAPABILITY_NAMES = frozenset(
    {
        "observation.turns",
        "observation.model_calls",
        "observation.tool_calls",
        "observation.subagents",
        "observation.policy_decisions",
        "observation.usage",
        "observation.outcomes",
        "enforcement.pre_request",
        "enforcement.pre_model",
        "enforcement.pre_tool",
        "enforcement.before_finish",
        "enforcement.interrupt",
        "enforcement.policy_failure",
        "lifecycle.sync",
        "lifecycle.async",
        "lifecycle.stream",
        "lifecycle.async_stream",
        "lifecycle.partial_close",
        "lifecycle.cancel",
        "lifecycle.resume",
        "lifecycle.fork",
        "lifecycle.subagent_handoff",
        "compatibility.model_override",
        "compatibility.tool_registry",
        "compatibility.mcp",
        "compatibility.acp",
        "compatibility.telemetry_export",
        "compatibility.context_handling",
        "compatibility.approval",
    }
)
_LIVE_ONLY = frozenset(
    {
        "compatibility.model_override",
        "compatibility.approval",
        "compatibility.context_handling",
        "lifecycle.resume",
        "lifecycle.fork",
    }
)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _fail(field: str, reason: str) -> None:
    raise ImportValidationError("invalid_capability", field, reason)


def _label(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 512:
        _fail(field, "must be a bounded nonempty string")


@dataclass(frozen=True)
class CapabilityDeclaration:
    name: str
    declared: bool | None
    boundaries: tuple[str, ...] = ()
    reason: str = "not declared"
    requires_live: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name not in CAPABILITY_NAMES:
            _fail("name", "unsupported capability dimension")
        if self.declared is not None and type(self.declared) is not bool:
            _fail("declared", "must be boolean or null")
        if type(self.requires_live) is not bool:
            _fail("requires_live", "must be boolean")
        if not isinstance(self.boundaries, (tuple, list)) or any(
            not isinstance(value, str) or not _ID.fullmatch(value) for value in self.boundaries
        ):
            _fail("boundaries", "must be bounded boundary identifiers")
        object.__setattr__(self, "boundaries", tuple(sorted(set(self.boundaries))))
        _label(self.reason, "reason")


@dataclass(frozen=True)
class AdapterManifest:
    adapter_id: str
    adapter_version: str
    upstream_harness: str
    upstream_version: str | None
    transport: str
    kind: str
    observation_source: str
    declaration_source: str
    capabilities: tuple[CapabilityDeclaration, ...]
    code_fingerprint: str | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        for name in (
            "adapter_id",
            "adapter_version",
            "upstream_harness",
            "transport",
            "observation_source",
            "declaration_source",
        ):
            _label(getattr(self, name), name)
        _label(self.upstream_version, "upstream_version", nullable=True)
        if (
            not isinstance(self.kind, str)
            or self.kind not in {"control", "observation"}
            or self.schema_version != "1.0"
        ):
            _fail("manifest", "unsupported adapter kind or version")
        if self.code_fingerprint is not None and (
            not isinstance(self.code_fingerprint, str) or not _HASH.fullmatch(self.code_fingerprint)
        ):
            _fail("code_fingerprint", "must be a SHA-256 digest or null")
        if not isinstance(self.capabilities, (tuple, list)):
            _fail("capabilities", "must be a declaration collection")
        values = tuple(self.capabilities)
        if any(not isinstance(item, CapabilityDeclaration) for item in values) or len(
            {item.name for item in values}
        ) != len(values):
            _fail("capabilities", "declarations must be typed and unique")
        if self.kind == "observation" and any(
            item.declared is True and item.name.startswith("enforcement.") for item in values
        ):
            _fail("capabilities", "observation adapters cannot declare runtime enforcement")
        object.__setattr__(self, "capabilities", tuple(sorted(values, key=lambda item: item.name)))

    def to_dict(self) -> dict:
        return json.loads(canonical_json(asdict(self)))

    @property
    def fingerprint(self) -> str:
        return sha256(canonical_json(self.to_dict()).encode()).hexdigest()

    @classmethod
    def from_dict(cls, value: dict) -> AdapterManifest:
        validate_json_tree(value)
        expected = {
            "schema_version",
            "adapter_id",
            "adapter_version",
            "upstream_harness",
            "upstream_version",
            "transport",
            "kind",
            "observation_source",
            "declaration_source",
            "capabilities",
            "code_fingerprint",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or not isinstance(value["capabilities"], list)
        ):
            _fail("manifest", "must contain exactly the documented fields")
        declarations = []
        for item in value["capabilities"]:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "declared",
                "boundaries",
                "reason",
                "requires_live",
            }:
                _fail("capabilities", "unsupported declaration fields")
            declarations.append(CapabilityDeclaration(**item))
        return cls(**{**value, "capabilities": tuple(declarations)})


@dataclass(frozen=True)
class CapabilityObservation:
    capability: str
    verdict: str
    probe_id: str | None
    probe_version: str | None
    tested_adapter_version: str | None
    tested_transport: str | None
    manifest_sha256: str | None
    evidence_ref: str | None
    evidence_sha256: str | None
    environment_fingerprint: str | None
    test_timestamp: str | None
    boundaries: tuple[str, ...] = ()
    source: str = "external_reported"
    reason: str | None = None
    _trusted: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, str)
            or self.capability not in CAPABILITY_NAMES
            or not isinstance(self.verdict, str)
            or self.verdict
            not in {
                "verified_supported",
                "verified_failed",
                "skipped",
                "unknown",
            }
        ):
            _fail("observation", "unsupported capability or verdict")
        if not isinstance(self.source, str) or self.source not in {
            "offline_behavioral",
            "live_behavioral",
            "external_reported",
        }:
            _fail("source", "unsupported evidence source")
        for name in (
            "probe_id",
            "probe_version",
            "tested_adapter_version",
            "tested_transport",
            "test_timestamp",
            "reason",
        ):
            _label(getattr(self, name), name, nullable=True)
        if self.test_timestamp is not None:
            try:
                parsed = datetime.fromisoformat(self.test_timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    _fail("test_timestamp", "must include a timezone")
            except ValueError as exc:
                if isinstance(exc, ImportValidationError):
                    raise
                _fail("test_timestamp", "must be an ISO timestamp")
        for name in ("manifest_sha256", "evidence_sha256", "environment_fingerprint"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
                _fail(name, "must be a SHA-256 digest or null")
        if self.evidence_ref is not None:
            relative_reference(self.evidence_ref)
        if self.verdict == "skipped" and self.reason is None:
            _fail("reason", "skipped probes require a reason")
        if not isinstance(self.boundaries, (tuple, list)) or any(
            not isinstance(value, str) or not _ID.fullmatch(value) for value in self.boundaries
        ):
            _fail("boundaries", "invalid observation boundaries")
        object.__setattr__(self, "boundaries", tuple(sorted(set(self.boundaries))))

    def to_dict(self) -> dict:
        value = asdict(self)
        value.pop("_trusted")
        return json.loads(canonical_json(value))

    @classmethod
    def from_dict(cls, value: dict) -> CapabilityObservation:
        validate_json_tree(value)
        expected = set(cls.__dataclass_fields__) - {"_trusted"}
        if not isinstance(value, dict) or set(value) != expected:
            _fail("observation", "must contain exactly the documented fields")
        # Deserialization preserves recorded claims, but does not turn a catalog
        # or historical report into a fresh trusted behavioral probe.
        return cls(**value)


def _probe_observation(**kwargs: Any) -> CapabilityObservation:
    """Internal bridge used only after an executable driver records its outcome."""
    result = CapabilityObservation(**kwargs)
    object.__setattr__(result, "_trusted", True)
    return result


def _source_hash(paths: Iterable[Path]) -> str | None:
    try:
        return sha256(
            canonical_json([sha256(path.read_bytes()).hexdigest() for path in paths]).encode()
        ).hexdigest()
    except OSError:
        return None


def builtin_adapters() -> tuple[AdapterManifest, ...]:
    """Enumerate core descriptors without SDK imports or plugin discovery."""
    from agentloop import harness
    from agentloop.integrations import langgraph_harness

    def control(adapter_id, capabilities, upstream, upstream_version, transport, paths):
        declarations = {
            name: CapabilityDeclaration(
                name,
                None,
                reason="not declared by the current wrapped-call contract",
                requires_live=name in _LIVE_ONLY,
            )
            for name in CAPABILITY_NAMES
        }
        for name, boundary in (
            ("enforcement.pre_model", "model"),
            ("enforcement.pre_tool", "tool"),
            ("enforcement.before_finish", "completion"),
        ):
            declarations[name] = CapabilityDeclaration(
                name,
                harness.Hook(boundary) in capabilities.hooks and "deny" in capabilities.actions,
                (boundary,),
                "explicitly wrapped boundary only",
            )
        for name, kind in (
            ("lifecycle.sync", "sync"),
            ("lifecycle.async", "async"),
            ("lifecycle.stream", "generator"),
            ("lifecycle.async_stream", "async_generator"),
        ):
            declarations[name] = CapabilityDeclaration(
                name,
                kind in capabilities.execution_kinds,
                ("wrapped_callable",),
                "explicit callable lifecycle; hidden SDK work is outside coverage",
            )
        for name in ("lifecycle.partial_close", "lifecycle.cancel", "enforcement.policy_failure"):
            declarations[name] = CapabilityDeclaration(
                name,
                True,
                ("wrapped_callable",),
                "declared wrapper lifecycle, awaiting behavioral probe",
            )
        for name in ("observation.policy_decisions", "observation.usage", "observation.outcomes"):
            declarations[name] = CapabilityDeclaration(
                name,
                True,
                ("wrapped_callable",),
                "hook evidence/explicit usage reader; not complete application capture",
            )
        return AdapterManifest(
            adapter_id,
            harness.CONTRACT_VERSION,
            upstream,
            upstream_version,
            transport,
            "control",
            "explicit_wrapped_calls",
            "agentloop.harness.AdapterCapabilities",
            tuple(declarations.values()),
            _source_hash(paths),
        )

    python = control(
        "python-wrapped",
        harness.PYTHON_CAPABILITIES,
        "python",
        None,
        "python-in-process",
        (Path(harness.__file__),),
    )
    langgraph = control(
        "langgraph-1.2.11",
        langgraph_harness.LangGraphHarness(boundaries=harness.BOUNDARIES).capabilities,
        "langgraph",
        langgraph_harness.SUPPORTED_VERSION,
        "langgraph-runnable",
        (Path(harness.__file__), Path(langgraph_harness.__file__)),
    )
    sources = []
    for adapter_id, system, module_path in (
        ("harbor-atif", "harbor", "integrations/harbor/atif.py"),
        ("omnigent-otel", "omnigent", "integrations/omnigent/telemetry.py"),
    ):
        declarations = []
        for name in sorted(CAPABILITY_NAMES):
            value = (
                False
                if name.startswith("enforcement.")
                else True
                if name
                in {
                    "observation.turns",
                    "observation.model_calls",
                    "observation.tool_calls",
                    "observation.usage",
                    "compatibility.telemetry_export",
                }
                or system == "omnigent"
                and name == "observation.policy_decisions"
                else None
            )
            declarations.append(
                CapabilityDeclaration(
                    name,
                    value,
                    ("file_import",),
                    "read-only source projection; no runtime interception or complete capture",
                    name in _LIVE_ONLY,
                )
            )
        sources.append(
            AdapterManifest(
                adapter_id,
                "1.0",
                system,
                None,
                "atif-file" if system == "harbor" else "otlp-file",
                "observation",
                "external_reported",
                module_path,
                tuple(declarations),
                _source_hash((Path(harness.__file__).parent / module_path,)),
            )
        )
    return (python, langgraph, *sources)


def get_adapter(adapter_id: str) -> AdapterManifest:
    for descriptor in builtin_adapters():
        if descriptor.adapter_id == adapter_id:
            return descriptor
    _fail("adapter_id", "adapter is not registered")


def load_manifest(path: str | Path) -> AdapterManifest:
    """Read a strict data-only description; never import its named adapter."""
    path = Path(path)
    return AdapterManifest.from_dict(load_json_artifact(path.parent, path.name).payload)


def capability_report(
    manifest: AdapterManifest, observations: Iterable[CapabilityObservation] = ()
) -> dict:
    by_name = {}
    for item in observations:
        if not isinstance(item, CapabilityObservation) or item.capability in by_name:
            _fail("observations", "observations must be typed and unique per capability")
        by_name[item.capability] = item
    if set(by_name) - {item.name for item in manifest.capabilities}:
        _fail("observations", "probe refers to an undeclared capability dimension")
    rows, drift = [], []
    for declaration in manifest.capabilities:
        observed = by_name.get(declaration.name)
        state = (
            "declared_supported"
            if declaration.declared is True
            else "declared_unsupported"
            if declaration.declared is False
            else "unknown"
        )
        reason = declaration.reason
        if observed is not None:
            missing = any(
                getattr(observed, key) is None
                for key in (
                    "probe_id",
                    "probe_version",
                    "tested_adapter_version",
                    "tested_transport",
                    "manifest_sha256",
                    "evidence_ref",
                    "evidence_sha256",
                    "environment_fingerprint",
                    "test_timestamp",
                )
            )
            current = (
                observed.tested_adapter_version == manifest.adapter_version
                and observed.tested_transport == manifest.transport
                and observed.manifest_sha256 == manifest.fingerprint
            )
            scope = set(declaration.boundaries) <= set(observed.boundaries)
            if observed.verdict == "skipped":
                state, reason = "skipped", observed.reason
            elif (
                missing
                or not current
                or not scope
                or not observed._trusted
                or observed.source == "external_reported"
            ):
                state, reason = (
                    "unknown",
                    "probe identity/evidence is incomplete, stale, out of scope or externally supplied",
                )
            elif (
                declaration.requires_live or declaration.name in _LIVE_ONLY
            ) and observed.source != "live_behavioral":
                state, reason = "unknown", "this capability requires a relevant live probe"
            else:
                state, reason = (
                    observed.verdict,
                    observed.reason or "current scoped behavioral probe",
                )
                if (
                    declaration.declared is True
                    and state == "verified_failed"
                    or declaration.declared is False
                    and state == "verified_supported"
                ):
                    drift.append(
                        {
                            "capability": declaration.name,
                            "declared": declaration.declared,
                            "observed": state,
                        }
                    )
        rows.append(
            {
                **asdict(declaration),
                "state": state,
                "reason": reason,
                "observation": observed.to_dict() if observed else None,
            }
        )
    return {
        "schema_version": "1.0",
        "adapter": manifest.to_dict(),
        "manifest_sha256": manifest.fingerprint,
        "capabilities": rows,
        "drift": drift,
        "drift_status": "failed" if drift else "no_observed_drift",
        "coverage": "explicit boundaries only; no claim of all application work",
    }


def require_verified_boundary(
    adapter_id: str, capability: str, observations: Iterable[CapabilityObservation]
) -> None:
    """Gate new registry-dependent controls on current built-in probe evidence."""
    manifest = get_adapter(adapter_id)
    if manifest.upstream_harness == "langgraph":
        from importlib.metadata import PackageNotFoundError, version

        try:
            installed = version("langgraph")
        except PackageNotFoundError:
            installed = None
        if installed != manifest.upstream_version:
            _fail("enforcement", "current upstream runtime version is unavailable or incompatible")
    report = capability_report(manifest, observations)
    row = next((row for row in report["capabilities"] if row["name"] == capability), None)
    if (
        manifest.kind != "control"
        or manifest.code_fingerprint is None
        or row is None
        or not capability.startswith("enforcement.")
        or row["declared"] is not True
        or row["state"] != "verified_supported"
    ):
        _fail("enforcement", "current adapter has no verified supported dispatch boundary")
