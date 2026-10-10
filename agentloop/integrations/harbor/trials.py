"""Completed Harbor job inventory, measured phases and external verifier data."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentloop.events import AgentEvent
from agentloop.integrations.harbor.atif import (
    ATIF_CONTRACT_REVISION,
    AtifImportResult,
    AtifOptions,
    _Importer,
    _native_bytes,
)
from agentloop.integrations.harbor.atif_validation import fail, label, timestamp
from agentloop.integrations.harbor.trial_evidence import attach_trial
from agentloop.integrations.harbor.verifier import (
    ScoringContract,
    external_outcome,
    reward_dimensions,
)
from agentloop.interoperability.contracts import (
    IDENTITY_FIELDS,
    ImportReceipt,
    external_id,
    receipt_id,
)
from agentloop.interoperability.validation import (
    ImportBudget,
    ImportLimits,
    ImportValidationError,
    JsonArtifact,
    indirect_path,
    load_json_artifact,
    relative_reference,
)
from agentloop.interventions import canonical_json
from agentloop.tracer import AgentTrace

TRIAL_CONTRACT_REVISION = "d5ac1be17f575852eaf4fffc4072fd18481c209b"
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def _object(value: Any, field_name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail(field_name, "metadata must be an object or null")
    return value


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("task_digest", "task digest must be a SHA-256 string")
    raw = value[7:] if value.startswith("sha256:") else value
    if not _HEX.fullmatch(raw):
        fail("task_digest", "task digest must use lowercase SHA-256")
    return "sha256:" + raw


def _phase(value: Any, field_name: str) -> dict:
    if value is None:
        return {
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "provenance": "unknown",
        }
    if not isinstance(value, dict):
        fail(field_name, "phase timing must be an object")
    start, finish = value.get("started_at"), value.get("finished_at")
    a = timestamp(start, field_name + ".started_at") if start is not None else None
    b = timestamp(finish, field_name + ".finished_at") if finish is not None else None
    duration = None
    if a is not None and b is not None and a.tzinfo is not None and b.tzinfo is not None:
        duration = (b - a).total_seconds() * 1000
        if duration < 0:
            fail(field_name, "phase ends before it begins")
    return {
        "started_at": start,
        "finished_at": finish,
        "duration_ms": duration,
        "provenance": "external_reported",
    }


def _usage(context: Any) -> dict:
    keys = {
        "input_tokens": "n_input_tokens",
        "output_tokens": "n_output_tokens",
        "cached_input_tokens": "n_cache_tokens",
        "cost_usd": "cost_usd",
    }
    if context is None:
        return dict.fromkeys(keys)
    if not isinstance(context, dict):
        fail("agent_result", "agent context must be an object")
    result = {target: context.get(source) for target, source in keys.items()}
    for key, value in result.items():
        if value is not None and (
            type(value) not in ({int, float} if key == "cost_usd" else {int})
            or not math.isfinite(value)
            or value < 0
        ):
            fail("agent_result." + key, "usage/cost must be finite nonnegative or null")
    if (
        result["input_tokens"] is not None
        and result["cached_input_tokens"] is not None
        and result["cached_input_tokens"] > result["input_tokens"]
    ):
        fail("agent_result", "cache tokens are an input subset")
    return result


def _exception(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("exception_info", "exception info must be an object")
    name = label(value.get("exception_type"), "exception_info.exception_type")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", name):
        fail("exception_info.exception_type", "exception class must be a bounded identifier")
    occurred = value.get("occurred_at")
    if occurred is not None:
        timestamp(occurred, "exception_info.occurred_at")
    return {
        "exception_type": name,
        "occurred_at": occurred,
        "message": {
            "capture": "omitted",
            "size_bytes": len(str(value.get("exception_message", "")).encode()),
        },
        "traceback": {
            "capture": "omitted",
            "size_bytes": len(str(value.get("exception_traceback", "")).encode()),
        },
    }


def _execution(exception: dict | None, phase: dict) -> str:
    if exception is not None:
        name = exception["exception_type"]
        if name == "CancelledError":
            return "cancelled"
        if "Timeout" in name or "TimedOut" in name:
            return "timed_out"
        return "failed"
    return (
        "completed"
        if phase["finished_at"] is not None
        else "partial"
        if phase["started_at"] is not None
        else "unknown"
    )


def _safe_reference(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("source_reference", "source reference must be a string")
    try:
        return {"relative_reference": relative_reference(value)}
    except ImportValidationError:
        return {"reference_sha256": sha256(value.encode()).hexdigest(), "capture": "omitted"}


@dataclass(frozen=True)
class HarborOptions:
    scoring: ScoringContract | None = None
    scoring_by_task_digest: dict[str, ScoringContract] = field(default_factory=dict)
    job_id: str | None = None
    condition: str | None = None
    protocol_id: str | None = None
    trial_context: dict[str, dict] = field(default_factory=dict)
    expected_hashes: dict[str, str] = field(default_factory=dict)
    continue_on_error: bool = True
    allow_legacy_results_name: bool = False
    synthetic_fixture: bool = False

    def __post_init__(self) -> None:
        if self.scoring is not None and not isinstance(self.scoring, ScoringContract):
            fail("options.scoring", "scoring must be a validated scoring contract")
        if not isinstance(self.scoring_by_task_digest, dict):
            fail("options.scoring_by_task_digest", "task scoring must be an object")
        for digest, scorer in self.scoring_by_task_digest.items():
            if _digest(digest) != digest or not isinstance(scorer, ScoringContract):
                fail(
                    "options.scoring_by_task_digest",
                    "require prefixed task digests and validated scoring contracts",
                )
        if not isinstance(self.trial_context, dict) or not isinstance(self.expected_hashes, dict):
            fail("options", "context and expected hashes must be objects")
        for name in ("continue_on_error", "allow_legacy_results_name", "synthetic_fixture"):
            if type(getattr(self, name)) is not bool:
                fail("options", "import flags must be boolean")
        for name in ("job_id", "condition", "protocol_id"):
            if getattr(self, name) is not None:
                label(getattr(self, name), "options." + name)
        for reference, digest in self.expected_hashes.items():
            relative_reference(reference)
            if not isinstance(digest, str) or not _HEX.fullmatch(digest):
                fail("expected_hashes", "expected artifact hashes must be SHA-256")
        for name, context in self.trial_context.items():
            # The empty key addresses a directly selected single-trial root.
            if name != "":
                relative_reference(name)
            if not isinstance(context, dict) or set(context) - {
                "condition",
                "protocol_id",
                "repetition",
            }:
                fail("trial_context", "only explicit condition/protocol/repetition are supported")
            for key, value in context.items():
                if key in {"condition", "protocol_id"}:
                    label(value, "trial_context." + key)
                if (
                    type(value) not in {str, int, float, bool}
                    or isinstance(value, float)
                    and not math.isfinite(value)
                ):
                    fail("trial_context", "context values must be finite typed scalars")


@dataclass(frozen=True)
class HarborImportResult:
    traces: tuple[AgentTrace, ...]
    trajectory_receipts: tuple[ImportReceipt, ...]
    trial_traces: tuple[AgentTrace, ...]
    trial_receipts: tuple[ImportReceipt, ...]
    _inventory_json: str

    def inventory(self) -> dict:
        return json.loads(self._inventory_json)

    def write(self, out: str | Path) -> Path:
        root = Path(out)
        # Reuse byte-bound export for both source projections and measured trials.
        AtifImportResult(
            (*self.traces, *self.trial_traces), (*self.trajectory_receipts, *self.trial_receipts)
        ).write(root)
        target = root / "harbor-inventory.json"
        content = (self._inventory_json + "\n").encode()
        if target.exists():
            if indirect_path(target) or target.read_bytes() != content:
                fail("output", "existing Harbor inventory conflicts", "output_conflict")
        else:
            with target.open("xb") as stream:
                stream.write(content)
        return target


class _JobImporter:
    def __init__(self, root: Path, options: HarborOptions, limits: ImportLimits):
        self.root, self.options, self.limits = root, options, limits
        self.budget = ImportBudget(limits)
        self.artifacts: dict[str, JsonArtifact] = {}
        self.rows: list[dict] = []
        self.traces: list[AgentTrace] = []
        self.trajectory_receipts: list[ImportReceipt] = []
        self.trial_traces: list[AgentTrace] = []
        self.trial_receipts: list[ImportReceipt] = []
        self.job_id = options.job_id
        self.job_id_provenance = "external_reported" if options.job_id else "calculated"
        self.producer_version = self.producer_revision = None
        self.job_witness: JsonArtifact | None = None
        self.job_notices: list[dict] = []

    def notice(
        self,
        notices: list,
        code: str,
        reference: str,
        field_name: str | None = None,
        severity: str = "warning",
    ):
        notices.append(
            {
                "code": code,
                "severity": severity,
                "artifact": reference,
                "field": field_name,
                "record": None,
            }
        )

    def load(self, reference: str, notices: list, *, optional: bool = True) -> JsonArtifact | None:
        try:
            if reference in self.artifacts:
                return self.artifacts[reference]
            artifact = load_json_artifact(
                self.root, reference, limits=self.limits, budget=self.budget
            )
            expected = self.options.expected_hashes.get(reference)
            if expected is not None and expected != artifact.artifact_sha256:
                fail(
                    "artifact_sha256",
                    "source hash differs from the expected artifact",
                    "hash_mismatch",
                )
            if not isinstance(artifact.payload, dict):
                fail("artifact", "Harbor metadata must be an object")
            self.artifacts[reference] = artifact
            return artifact
        except ImportValidationError as exc:
            if exc.code == "limit_exceeded" or (
                not self.options.continue_on_error and exc.code != "missing_artifact"
            ):
                raise
            if exc.code == "missing_artifact" and reference in self.options.expected_hashes:
                self.notice(notices, "expected_artifact_missing", reference, exc.field, "error")
            if not optional or exc.code != "missing_artifact":
                self.notice(notices, exc.code, reference, exc.field, "error")
            return None

    def discover(self) -> tuple[list[str], dict, list[dict]]:
        notices = []
        config = self.load("config.json", notices)
        lock = self.load("lock.json", notices)
        result = self.load("result.json", notices)
        self.job_witness = result or config or lock
        job = result.payload if result is not None else {}
        single = ("trial_name" in job and "task_checksum" in job) or (
            config is not None and "task" in config.payload and "agent" in config.payload
        )
        if self.job_id is None:
            raw = None if single else job.get("id")
            self.job_id = (
                label(raw, "job.id")
                if raw is not None
                else external_id(
                    "group",
                    "harbor",
                    {
                        "selected_directory": self.root.name,
                        "config_sha256": config.artifact_sha256 if config else None,
                        "lock_sha256": lock.artifact_sha256 if lock else None,
                    },
                )
            )
            if raw is not None:
                self.job_id_provenance = "external_reported"
        if lock is not None and isinstance(lock.payload.get("harbor"), dict):
            info = lock.payload["harbor"]
            self.producer_version = (
                label(info["version"], "harbor.version")
                if info.get("version") is not None
                else None
            )
            revision = info.get("git_commit_hash")
            self.producer_revision = (
                revision
                if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
                else None
            )
        names = []
        ignored = []
        if single:
            names = [""]
        else:
            seen = set()
            for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
                if not entry.is_dir():
                    continue
                name = relative_reference(entry.name, self.limits)
                if "/" in name:
                    fail("trial_name", "trial names must be single path components")
                if indirect_path(entry):
                    ignored.append({"directory": name, "reason": "unsafe_directory"})
                    self.notice(notices, "unsafe_directory", name, severity="error")
                    continue
                recognized = any(
                    (entry / file).exists()
                    for file in ("result.json", "config.json", "lock.json", "results.json")
                )
                if not recognized:
                    ignored.append({"directory": name, "reason": "no_trial_metadata"})
                    continue
                key = unicodedata.normalize("NFC", name).casefold()
                if key in seen:
                    fail("trial_name", "trial directory names collide across supported filesystems")
                seen.add(key)
                names.append(name)
            embedded = job.get("trial_results")
            if embedded is None:
                embedded = []
            if not isinstance(embedded, list):
                fail("job.trial_results", "trial_results must be an array")
            for item in embedded:
                if not isinstance(item, dict):
                    fail("job.trial_results", "trial results must be objects")
                name = relative_reference(label(item.get("trial_name"), "trial_name"), self.limits)
                if "/" in name:
                    fail("trial_name", "trial names must be single path components")
                if name not in names:
                    names.append(name)
        if len(names) > self.limits.max_trials:
            fail("trials", "trial count exceeds configured bounds", "limit_exceeded")
        if not names and not (job and job.get("n_total_trials") is not None):
            fail("job", "no recognizable Harbor trials were found")
        self.job_notices = notices
        return (
            names,
            {"metadata": job, "ignored_directories": ignored, "notices": notices},
            (job.get("trial_results") or []) if not single else [],
        )

    def trial(self, name: str, embedded: dict | None) -> None:
        self.budget.consume(trials=1)
        prefix = name + "/" if name else ""
        notices = list(self.job_notices)
        result_artifact = self.load(prefix + "result.json", notices)
        if (
            result_artifact is None
            and not (self.root / (prefix + "result.json")).exists()
            and self.options.allow_legacy_results_name
        ):
            result_artifact = self.load(prefix + "results.json", notices)
        elif (self.root / (prefix + "results.json")).exists():
            self.notice(notices, "legacy_result_name_not_used", prefix + "results.json")
        config_artifact = self.load(prefix + "config.json", notices)
        lock_artifact = self.load(prefix + "lock.json", notices)
        witness = result_artifact or config_artifact or lock_artifact or self.job_witness
        if witness is None:
            self.rows.append(
                {
                    "trial_directory": name,
                    "trace_files": [],
                    "receipt_id": None,
                    "import_status": "missing",
                    "reason": "no_source_metadata",
                }
            )
            return
        result = result_artifact.payload if result_artifact is not None else embedded or {}
        config = (
            config_artifact.payload
            if config_artifact is not None
            else _object(result.get("config"), "config")
        )
        lock = lock_artifact.payload if lock_artifact is not None else {}
        if lock and (
            type(lock.get("schema_version")) is not int or lock["schema_version"] not in {2, 3}
        ):
            fail("lock.schema_version", "only trial lock schema 2/3 projections are supported")
        if not isinstance(config, dict):
            fail("config", "trial config must be an object")
        raw_id = result.get("id")
        trial_id = (
            label(raw_id, "trial.id")
            if raw_id is not None
            else external_id(
                "group",
                "harbor",
                {"job_id": self.job_id, "trial_directory": name or self.root.name},
            )
        )
        checksum = _digest(result.get("task_checksum"))
        task_lock = _object(lock.get("task"), "lock.task")
        if not isinstance(task_lock, dict):
            fail("lock.task", "task lock must be an object")
        locked_digest = _digest(task_lock.get("digest"))
        if checksum is not None and locked_digest is not None and checksum != locked_digest:
            self.notice(
                notices, "task_digest_mismatch", witness.reference, "task_checksum", "error"
            )
        task_digest = checksum or locked_digest
        scoring = self.options.scoring_by_task_digest.get(task_digest, self.options.scoring)
        task_id_data = _object(result.get("task_id"), "task_id")
        if not isinstance(task_id_data, dict):
            fail("task_id", "task identity must be an object")
        task_id = (
            external_id("group", "harbor", {"task_identity_sha256": _hash(task_id_data)})
            if task_id_data
            else None
        )
        identity = dict.fromkeys(sorted(IDENTITY_FIELDS))
        identity.update(
            job_id=self.job_id, trial_id=trial_id, task_id=task_id, task_digest=task_digest
        )
        provenance = {
            key: "external_reported" if value is not None else "unknown"
            for key, value in identity.items()
        }
        provenance["task_id"] = "calculated" if task_id is not None else "unknown"
        provenance["job_id"] = self.job_id_provenance
        if raw_id is None:
            provenance["trial_id"] = "calculated"
        phase = _phase(result.get("agent_execution"), "agent_execution")
        exception = _exception(result.get("exception_info"))
        execution = _execution(exception, phase)
        if not result:
            execution = "missing"
            self.notice(notices, "missing_trial_result", prefix + "result.json")
        for key in ("trial_name", "task_name"):
            if result.get(key) is not None:
                label(result[key], key)
        steps = result.get("step_results")
        if steps is None:
            steps = []
        if not isinstance(steps, list):
            fail("step_results", "step results must be an array")
        step_rows = []
        step_names = set()
        for step in steps:
            if not isinstance(step, dict):
                fail("step_results", "step result must be an object")
            step_name = relative_reference(label(step.get("step_name"), "step_name"), self.limits)
            key = unicodedata.normalize("NFC", step_name).casefold()
            if "/" in step_name or key in step_names:
                fail("step_name", "step names must be distinct portable components")
            step_names.add(key)
            step_rows.append((step_name, step))
        usage = _usage(result.get("agent_result"))
        if result.get("agent_result") is None and step_rows:
            contexts = [_usage(step.get("agent_result")) for _, step in step_rows]
            usage = {
                key: sum(item[key] for item in contexts)
                if all(item[key] is not None for item in contexts)
                else None
                for key in usage
            }
        isolation = result.get("verifier_environment_mode")
        if isolation is None:
            isolation = _object(lock.get("verifier"), "lock.verifier").get("environment_mode")
        if isolation is None:
            isolation = "unknown"
        if not isinstance(isolation, str) or isolation not in {"shared", "separate", "unknown"}:
            fail("verifier_environment_mode", "unsupported verifier isolation")
        quality_status, dimensions = self.verifier(
            prefix, result.get("verifier_result"), notices, exception
        )
        step_details = []
        trajectory_paths = []
        if step_rows:
            for step_name, step in step_rows:
                step_prefix = prefix + "steps/" + step_name + "/"
                step_config = self.load(step_prefix + "config.json", notices)
                configured_mode = (
                    _object(step_config.payload.get("verifier"), "step.config.verifier").get(
                        "environment_mode"
                    )
                    if step_config
                    else None
                ) or "unknown"
                # Current Harbor StepResult does not record resolved isolation.
                # A requested setting alone is not observed execution metadata.
                mode = step.get("verifier_environment_mode")
                if mode is None:
                    mode = "unknown"
                if not isinstance(mode, str) or mode not in {"shared", "separate", "unknown"}:
                    fail("step.verifier_environment_mode", "unsupported step verifier isolation")
                step_exception = _exception(step.get("exception_info"))
                step_phase = _phase(step.get("agent_execution"), "step.agent_execution")
                status, scores = self.verifier(
                    step_prefix, step.get("verifier_result"), notices, step_exception
                )
                step_details.append(
                    {
                        "step_name": step_name,
                        "agent_execution": step_phase,
                        "verifier_timing": _phase(step.get("verifier"), "step.verifier"),
                        "configured_verifier_mode": configured_mode,
                        "exception_info": step_exception,
                        "usage": _usage(step.get("agent_result")),
                        "verifier_rewards": scores,
                        "outcome": external_outcome(
                            execution_status=_execution(step_exception, step_phase),
                            verifier_status=status,
                            dimensions=scores,
                            isolation=mode,
                            scoring=scoring,
                        ),
                    }
                )
                trajectory_paths.append((step_name, step_prefix + "agent/trajectory.json"))
            isolation = (
                "mixed"
                if len({item["outcome"]["verifier_isolation"] for item in step_details}) > 1
                else step_details[0]["outcome"]["verifier_isolation"]
            )
            if result.get("verifier_result") is None:
                # Per-step results retain their individual dimensions; no invented
                # scalar reward or silent averaging across scales.
                dimensions = {}
                quality_status = "missing"
            elif any(
                item["outcome"]["verifier_status"] in {"failed", "invalid"}
                or item["outcome"]["quality_pass"] is False
                for item in step_details
            ):
                quality_status = "invalid"
                self.notice(
                    notices,
                    "trial_step_quality_conflict",
                    witness.reference,
                    "step_results",
                    "error",
                )
        else:
            trajectory_paths = [(None, prefix + "agent/trajectory.json")]
        outcome = external_outcome(
            execution_status=execution,
            verifier_status=quality_status,
            dimensions=dimensions,
            isolation=isolation,
            scoring=scoring,
        )
        if step_details and result.get("verifier_result") is None and scoring is not None:
            # Preserve per-step predicates; no trial-level scoring rule is inferred.
            outcome["quality_pass"] = None  # trial has no compatible trial-level reward contract
            self.notice(
                notices, "step_quality_requires_trial_protocol", witness.reference, "step_results"
            )
        imported = []
        for step_name, reference in trajectory_paths:
            importer = _Importer(
                self.root,
                self.limits,
                AtifOptions(
                    strict=not self.options.continue_on_error,
                    synthetic_fixture=self.options.synthetic_fixture,
                ),
                {
                    key: value
                    for key, value in {**identity, "step_id": step_name}.items()
                    if key in {"job_id", "trial_id", "task_id", "task_digest", "step_id"}
                    and value is not None
                },
            )
            try:
                # All job records share limits, including reference traversals.
                importer.budget = self.budget
                importer.file(reference)
            except ImportValidationError as exc:
                if exc.code == "limit_exceeded" or not self.options.continue_on_error:
                    raise
                self.notice(
                    notices,
                    exc.code,
                    reference,
                    exc.field,
                    "warning" if exc.code == "missing_artifact" else "error",
                )
                if reference in self.options.expected_hashes:
                    self.notice(
                        notices, "expected_artifact_unavailable", reference, severity="error"
                    )
            for source_receipt in importer.receipts:
                source = source_receipt.to_dict()
                source_ref = source["source"]["artifact_reference"]
                expected = self.options.expected_hashes.get(source_ref)
                if expected is not None and expected != source["source"]["artifact_sha256"]:
                    self.notice(notices, "hash_mismatch", source_ref, "artifact_sha256", "error")
                if source["missing_trace_reason"] == "invalid_artifact":
                    self.notice(notices, "invalid_trajectory", source_ref, severity="error")
            imported.extend(importer.traces)
            self.traces.extend(importer.traces)
            self.trajectory_receipts.extend(importer.receipts)
        if not imported:
            self.notice(notices, "missing_trajectory", witness.reference)
        if (
            embedded is not None
            and result_artifact is not None
            and embedded != result_artifact.payload
        ):
            self.notice(
                notices,
                "embedded_trial_result_conflict",
                witness.reference,
                "trial_results",
                "error",
            )
        errors = any(notice["severity"] == "error" for notice in notices)
        if errors and not self.options.continue_on_error:
            fail("trial", "source metadata is invalid or contradictory")
        if errors:
            outcome = external_outcome(
                execution_status=execution,
                verifier_status="invalid",
                dimensions=dimensions,
                isolation=isolation,
                scoring=scoring,
            )
        receipt = {
            "schema_version": "1.0",
            "receipt_id": "pending",
            "source": {
                "system": "harbor",
                "producer_version": self.producer_version,
                "producer_revision": self.producer_revision,
                "format": "harbor_trial",
                "format_version": None,
                "artifact_reference": witness.reference,
                "artifact_sha256": witness.artifact_sha256,
                "trust": "external_reported",
            },
            "external_identity": identity,
            "identity_provenance": provenance,
            "traces": [],
            "missing_trace_reason": "invalid_artifact" if errors else "missing_artifact",
            "outcome": outcome,
            "completeness": {
                "trajectory": "complete" if imported else "missing",
                "usage": "complete"
                if usage["input_tokens"] is not None and usage["output_tokens"] is not None
                else "partial",
                "quality": "complete" if outcome["verifier_status"] == "available" else "missing",
                "parentage": "unknown",
                "timing": "complete" if phase["duration_ms"] is not None else "missing",
                "cost": "complete" if usage["cost_usd"] is not None else "unknown",
            },
            "relationships": [],
            "notices": notices,
            "source_metadata": {
                "trial_directory": name,
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "task_type": task_lock.get("type")
                or (
                    "git"
                    if "git_url" in task_id_data
                    else "package"
                    if "org" in task_id_data
                    else "local"
                    if "path" in task_id_data
                    else "unknown"
                ),
                "task_reference": {
                    "path": _safe_reference(task_id_data.get("path")),
                    "git_url": _safe_reference(task_id_data.get("git_url")),
                    "git_commit_id": task_id_data.get("git_commit_id"),
                    "org": task_id_data.get("org"),
                    "name": task_id_data.get("name"),
                    "ref": task_id_data.get("ref"),
                },
                "agent_info": self.agent_info(result.get("agent_info")),
                "source": _safe_reference(result.get("source")),
                "source_trial": self.source_trial(
                    lock.get("source_trial") or config.get("source_trial")
                ),
                "environment": {
                    "type": _object(
                        lock.get("environment") or config.get("environment"), "environment"
                    ).get("type"),
                    "config_sha256": _hash(
                        lock.get("environment") or config.get("environment") or {}
                    ),
                },
                "phase_timing": {
                    key: _phase(result.get(key), key)
                    for key in ("agent_execution", "agent_setup", "environment_setup", "verifier")
                },
                "exception_info": exception,
                "usage": usage,
                "trial_wall_timing": _phase(
                    {
                        "started_at": result.get("started_at"),
                        "finished_at": result.get("finished_at"),
                    },
                    "trial_wall_timing",
                ),
                "verifier_config_hash": _hash(
                    {
                        "requested": config.get("verifier"),
                        "resolved_lock": lock.get("verifier"),
                        "isolation": isolation,
                    }
                ),
                "agent_config_hash": _hash(config.get("agent") or {}),
                "dataset": _safe_reference(result.get("source")),
                "dataset_version": result.get("dataset_version"),
                "verifier_rewards": dimensions,
                "usage_scope": "agent context, including reported subagents; setup/verifier scope not inferred",
                "artifact_hashes": {
                    key: item.artifact_sha256
                    for key, item in self.artifacts.items()
                    if key.startswith(prefix)
                },
                "step_results": step_details,
                "adapter_contract_revision": TRIAL_CONTRACT_REVISION,
                "trajectory_run_ids": [trace.run_id for trace in imported],
                "source_results_present": result_artifact is not None,
            },
        }
        receipt["receipt_id"] = receipt_id(receipt)
        context = {
            "condition": self.options.condition or result.get("condition"),
            "protocol_id": self.options.protocol_id or result.get("protocol_id"),
            "repetition": result.get("repetition"),
            **self.options.trial_context.get(name, {}),
        }
        environment = lock.get("environment") or config.get("environment") or {}
        pairing = {
            "task_digest": task_digest,
            "scorer_config_hash": scoring.to_dict()["config_sha256"] if scoring else None,
            "environment_hash": _hash(environment) if environment else None,
            "protocol_id": context.get("protocol_id"),
            "repetition": context.get("repetition"),
        }
        if imported and not errors:
            run_id = external_id(
                "run",
                "harbor",
                {
                    "job_id": self.job_id,
                    "trial_id": trial_id,
                    "scope": "agent_execution_measurement",
                },
            )
            trace = AgentTrace(
                name=result.get("trial_name") or name or "harbor_trial",
                run_id=run_id,
                started_at=phase["started_at"] or "unknown",
                ended_at=phase["finished_at"],
                elapsed_ms=phase["duration_ms"],
                metadata={
                    "source": "harbor_trial",
                    **pairing,
                    "condition": context.get("condition"),
                    "source_receipt_id": receipt["receipt_id"],
                    **({"synthetic": True} if self.options.synthetic_fixture else {}),
                },
            )
            trace.events = [
                AgentEvent(
                    event_id=external_id(
                        "event", "harbor", {"run_id": run_id, "scope": "agent_execution"}
                    ),
                    run_id=run_id,
                    event_type="source_phase",
                    name="agent_execution",
                    started_at=phase["started_at"] or "unknown",
                    ended_at=phase["finished_at"] or "unknown",
                    duration_ms=phase["duration_ms"] or 0.0,
                    status="error" if execution in {"failed", "timed_out", "cancelled"} else "ok",
                    error="External agent phase did not complete"
                    if execution in {"failed", "timed_out", "cancelled"}
                    else None,
                    metadata={
                        "operation_kind": "agent",
                        "external_evidence_schema": "1.0",
                        "timing_available": phase["duration_ms"] is not None,
                        "source_status_available": execution
                        in {"completed", "failed", "timed_out", "cancelled"},
                        "source_execution_status": execution,
                        "error_type": exception["exception_type"] if exception else None,
                    },
                )
            ]
            attach_trial(
                trace,
                {
                    "receipt_id": receipt["receipt_id"],
                    "source_result": {
                        "reference": witness.reference,
                        "sha256": witness.artifact_sha256,
                    },
                    "measurement": {
                        "runtime_ms": phase["duration_ms"],
                        "scope": "agent_execution",
                        **usage,
                        "usage_provenance": "external_reported",
                        "cost_scope": "agent_context",
                    },
                    "outcome": outcome,
                    "pairing": pairing,
                },
            )
            self.trial_traces.append(trace)
            receipt["traces"] = [
                {
                    "run_id": trace.run_id,
                    "trace_file": f"traces/{trace.run_id}.json",
                    "trace_sha256": sha256(_native_bytes(trace)).hexdigest(),
                }
            ]
            receipt["missing_trace_reason"] = None
        frozen = ImportReceipt.from_dict(receipt, limits=self.limits)
        self.trial_receipts.append(frozen)
        self.rows.append(
            {
                "trial_directory": name,
                "trial_id": trial_id,
                "receipt_id": frozen.receipt_id,
                "trace_files": [item["trace_file"] for item in receipt["traces"]],
                "trajectory_run_ids": [trace.run_id for trace in imported],
                "import_status": "invalid"
                if errors
                else "imported"
                if imported
                else "missing_trajectory",
                "execution_status": execution,
                "verifier_status": outcome["verifier_status"],
                "quality_pass": outcome["quality_pass"],
                "pairing": pairing,
                "condition": context.get("condition"),
            }
        )

    def verifier(
        self, prefix: str, embedded: Any, notices: list, exception: dict | None
    ) -> tuple[str, dict | None]:
        dimensions = None
        status = "missing"
        try:
            if embedded is not None:
                if not isinstance(embedded, dict):
                    fail("verifier_result", "verifier result must be an object")
                dimensions = reward_dimensions(embedded.get("rewards"))
                status = "available" if dimensions is not None else "missing"
        except ImportValidationError as exc:
            self.notice(notices, exc.code, prefix + "result.json", exc.field, "error")
            status = "invalid"
        reference = prefix + "verifier/reward.json"
        file = self.load(reference, notices)
        if file is not None:
            try:
                rewards = reward_dimensions(file.payload)
                if dimensions is not None and rewards != dimensions:
                    self.notice(notices, "reward_result_conflict", reference, "rewards", "error")
                    status = "invalid"
                elif status != "invalid":
                    dimensions, status = rewards, "available"
            except ImportValidationError as exc:
                self.notice(notices, exc.code, reference, exc.field, "error")
                status = "invalid"
        if any(
            notice["artifact"] == reference and notice["severity"] == "error" for notice in notices
        ):
            status = "invalid"
        if exception is not None and "Verifier" in exception["exception_type"]:
            status = "failed"
        return status, dimensions

    def agent_info(self, value: Any) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            fail("agent_info", "agent info must be an object")
        model = value.get("model_info") or {}
        if not isinstance(model, dict):
            fail("agent_info.model_info", "model info must be an object")
        return {
            "name": value.get("name"),
            "version": value.get("version"),
            "model_info": {"name": model.get("name"), "provider": model.get("provider")},
        }

    def source_trial(self, value: Any) -> dict | None:
        if value is None:
            return None
        source = _object(value, "source_trial")
        return {
            "action": source.get("action"),
            "type": source.get("type"),
            "trial_id": source.get("trial_id"),
            "path": _safe_reference(source.get("path")),
        }

    def run(self) -> HarborImportResult:
        names, job, embedded = self.discover()
        embedded_by_name = {}
        for item in embedded:
            name = item["trial_name"]
            if name in embedded_by_name:
                fail("trial_results", "duplicate embedded trial name")
            embedded_by_name[name] = item
        for name in names:
            destinations = (
                self.traces,
                self.trajectory_receipts,
                self.trial_traces,
                self.trial_receipts,
                self.rows,
            )
            before = [len(items) for items in destinations]
            try:
                self.trial(name, embedded_by_name.get(name))
            except ImportValidationError as exc:
                if not self.options.continue_on_error or exc.code == "limit_exceeded":
                    raise
                # A late receipt/measurement failure must not leave orphan native
                # projections or inflate counts in an otherwise partial import.
                for items, length in zip(destinations, before):
                    del items[length:]
                self.rows.append(
                    {
                        "trial_directory": name,
                        "receipt_id": None,
                        "trace_files": [],
                        "import_status": "invalid",
                        "error": exc.to_dict(),
                    }
                )
        ids = [row["trial_id"] for row in self.rows if row.get("trial_id") is not None]
        if len(ids) != len(set(ids)):
            fail("trial.id", "duplicate trial identity within one job")
        planned = job["metadata"].get("n_total_trials")
        if planned is not None and (
            type(planned) is not int or planned < 0 or planned > self.limits.max_trials
        ):
            fail("n_total_trials", "planned trial count outside configured bounds")
        inventory = {
            "schema_version": "1.0",
            "source": "harbor",
            "synthetic": self.options.synthetic_fixture,
            "job_id": self.job_id,
            "source_versions": {
                "producer_version": self.producer_version,
                "producer_revision": self.producer_revision,
                "adapter_contract_revision": TRIAL_CONTRACT_REVISION,
                "atif_contract_revision": ATIF_CONTRACT_REVISION,
            },
            "trials_discovered": len(names),
            "trials_planned": planned,
            "planned_unidentified_trials": max(0, planned - len(names))
            if planned is not None
            else None,
            "trials_imported": sum(row["import_status"] == "imported" for row in self.rows),
            "missing_trajectories": sum(
                row["import_status"] == "missing_trajectory" for row in self.rows
            ),
            "invalid_records": sum(row["import_status"] == "invalid" for row in self.rows),
            "verifier_available": sum(
                row.get("verifier_status") == "available" for row in self.rows
            ),
            "external_quality_uninterpreted": sum(
                receipt.to_dict()["outcome"]["quality_basis"] == "external_uninterpreted_reward"
                for receipt in self.trial_receipts
            ),
            "quality_pass": sum(row.get("quality_pass") is True for row in self.rows),
            "quality_fail": sum(row.get("quality_pass") is False for row in self.rows),
            "quality_indeterminate": sum(row.get("quality_pass") is None for row in self.rows),
            "trial_rows": self.rows,
            "job_notices": job["notices"],
            "ignored_directories": job["ignored_directories"],
            "job_artifact_hashes": {
                name: self.artifacts[name].artifact_sha256
                for name in ("result.json", "config.json", "lock.json")
                if name in self.artifacts
            },
            "job_timing": None
            if "trial_name" in job["metadata"] and "task_checksum" in job["metadata"]
            else _phase(
                {
                    "started_at": job["metadata"].get("started_at"),
                    "finished_at": job["metadata"].get("finished_at"),
                },
                "job_timing",
            ),
            "job_timing_scope": "selected_job_metadata; single-trial roots are not job orchestration measurements",
            "job_concurrency": self.artifacts["config.json"].payload.get("n_concurrent_trials")
            if "config.json" in self.artifacts
            else None,
        }
        return HarborImportResult(
            tuple(self.traces),
            tuple(self.trajectory_receipts),
            tuple(self.trial_traces),
            tuple(self.trial_receipts),
            canonical_json(inventory),
        )


def import_harbor(
    path: str | Path,
    *,
    options: HarborOptions = HarborOptions(),
    limits: ImportLimits = ImportLimits(),
) -> HarborImportResult:
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        fail("path", "Harbor import requires a completed job/trial directory")
    return _JobImporter(root, options, limits).run()
