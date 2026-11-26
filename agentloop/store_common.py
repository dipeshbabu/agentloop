"""Helpers shared by the SQLite and Postgres trace stores.

Split out of ``agentloop.store`` (issue #407); ``agentloop.store`` re-exports
every name, so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agentloop.ranking import cluster_ranking, finalize_ranking
from agentloop.savings import SavingsItem, Selection, select_compatible
from agentloop.store_keys import (
    verify_api_key_hash,
)
from agentloop.store_types import (
    _COST_STATUSES,
    _MAX_DATABASE_INTEGER,
    ALLOWED_FINDING_TRANSITIONS,
    FINDING_STATUSES,
    MAX_PAGE_SIZE,
)

# Stored key hashes that a presented key has matched in this process, mapped to
# that key. scrypt costs tens of milliseconds by design, and every
# authenticated request presents its key. Entries are looked up from the hash
# rows read for each request, so a revoked key, whose row is gone, no longer
# matches. The process already receives each key in plain text per request.
_VERIFIED_KEYS: dict[str, str] = {}
_VERIFIED_KEYS_LIMIT = 1024


def _find_api_key_row(api_key: str, rows: list[Any], key_hash_of: Any) -> Any | None:
    for row in rows:
        known = _VERIFIED_KEYS.get(key_hash_of(row))
        # Compare bytes: compare_digest rejects non-ASCII str arguments.
        if known is not None and hmac.compare_digest(
            known.encode("utf-8"), api_key.encode("utf-8")
        ):
            return row
    for row in rows:
        if verify_api_key_hash(api_key, key_hash_of(row)):
            if len(_VERIFIED_KEYS) >= _VERIFIED_KEYS_LIMIT:
                _VERIFIED_KEYS.clear()
            _VERIFIED_KEYS[key_hash_of(row)] = api_key
            return row
    return None


_DELETE_CHUNK = 500


def _retention_cutoff(older_than_days: Any) -> datetime:
    if (
        isinstance(older_than_days, bool)
        or not isinstance(older_than_days, (int, float))
        or not math.isfinite(older_than_days)
        or older_than_days <= 0
    ):
        raise ValueError("Retention must be a positive number of days")
    return datetime.now(timezone.utc) - timedelta(days=float(older_than_days))


def _delete_runs(conn, mark: str, project_id: str, run_ids: list[str]) -> dict[str, int]:
    """Delete traces and everything derived from them, children first.

    Interventions that compare a deleted trace go too, since their evidence is
    gone. Counts are per table.
    """
    counts = {"traces": 0, "findings": 0, "usage_events": 0, "interventions": 0}
    for start in range(0, len(run_ids), _DELETE_CHUNK):
        chunk = run_ids[start : start + _DELETE_CHUNK]
        marks = ", ".join([mark] * len(chunk))
        statements = (
            (
                "interventions",
                f"DELETE FROM interventions WHERE project_id = {mark} AND "  # nosec B608
                f"(baseline_run_id IN ({marks}) OR candidate_run_id IN ({marks}))",
                (project_id, *chunk, *chunk),
            ),
            (
                "findings",
                f"DELETE FROM trace_findings WHERE project_id = {mark} AND run_id IN ({marks})",  # nosec B608
                (project_id, *chunk),
            ),
            (
                "usage_events",
                f"DELETE FROM usage_events WHERE project_id = {mark} AND run_id IN ({marks})",  # nosec B608
                (project_id, *chunk),
            ),
            (
                "traces",
                f"DELETE FROM traces WHERE project_id = {mark} AND run_id IN ({marks})",  # nosec B608
                (project_id, *chunk),
            ),
        )
        for name, sql, params in statements:
            # The statements above interpolate only "?"/"%s" placeholders, never values.
            cursor = conn.execute(sql, params)
            counts[name] += int(cursor.rowcount or 0)
    return counts


LABEL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_LABEL_VALUE_LENGTH = 500


def validate_label_filter(label: Any) -> tuple[str, str]:
    """Check a (key, value) label filter; keys use letters, digits, '.', '_' and '-'."""
    if (
        not isinstance(label, tuple)
        or len(label) != 2
        or not all(isinstance(part, str) for part in label)
        or not LABEL_KEY_PATTERN.match(label[0])
        or len(label[1]) > MAX_LABEL_VALUE_LENGTH
    ):
        raise ValueError(
            "Label filters are KEY=VALUE with a key of letters, digits, '.', '_' or '-'"
        )
    return label


def parse_label_filter(text: str) -> tuple[str, str]:
    """Parse ``KEY=VALUE`` into a validated label filter."""
    key, separator, value = text.partition("=") if isinstance(text, str) else ("", "", "")
    if not separator:
        raise ValueError(
            "Label filters are KEY=VALUE with a key of letters, digits, '.', '_' or '-'"
        )
    return validate_label_filter((key, value))


def _with_labels(item: dict[str, Any]) -> dict[str, Any]:
    labels = item.get("labels")
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except ValueError:
            labels = None
    item["labels"] = (
        {str(key): value for key, value in labels.items()} if isinstance(labels, dict) else {}
    )
    return item


def _record_audit(
    conn, mark: str, action: str, project_id: str | None, actor: str, target: str | None, details
) -> None:
    """Append an audit event in the caller's transaction."""
    conn.execute(
        "INSERT INTO audit_events(project_id, action, actor, target, details_json) "  # nosec B608
        f"VALUES ({mark}, {mark}, {mark}, {mark}, {mark})",
        (
            project_id,
            action,
            str(actor or "unknown")[:200],
            target,
            json.dumps(details or {}, sort_keys=True),
        ),
    )


def _audit_row(row: tuple[Any, ...]) -> dict[str, Any]:
    event_id, occurred_at, project_id, action, actor, target, details_json = row
    try:
        details = json.loads(details_json) if details_json else {}
    except ValueError:
        details = {}
    return {
        "id": int(event_id),
        "occurred_at": _timestamp_text(occurred_at),
        "project_id": project_id,
        "action": action,
        "actor": actor,
        "target": target,
        "details": details,
    }


def _timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _validate_page_limit(limit: int) -> int:
    if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"page limit must be between 1 and {MAX_PAGE_SIZE}, got {limit!r}")
    return limit


def _validate_new_status(new_status: str) -> str:
    if new_status not in FINDING_STATUSES:
        raise ValueError(
            f"unknown finding status {new_status!r}; expected one of {sorted(FINDING_STATUSES)}"
        )
    return new_status


def _sources_allowing(new_status: str) -> list[str]:
    return [
        source for source, targets in ALLOWED_FINDING_TRANSITIONS.items() if new_status in targets
    ]


# The widest source set any target status has (detected <- resolved/dismissed/superseded).
# update_finding_status() pads its source list to this fixed width so the SQL text is
# static regardless of new_status, rather than building a variable-length `IN (...)`.
_MAX_TRANSITION_SOURCES = max(len(_sources_allowing(status)) for status in FINDING_STATUSES)


def _padded_sources(new_status: str) -> list[str]:
    sources = _sources_allowing(new_status)
    return sources + [""] * (_MAX_TRANSITION_SOURCES - len(sources))


def _finding_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_payload = row.pop("payload_json")
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    row["patchable"] = bool(row["patchable"])
    row["estimated_latency_savings_ms"] = (
        None
        if row["estimated_latency_savings_ms"] is None
        else float(row["estimated_latency_savings_ms"] or 0)
    )
    row["estimated_cost_savings_usd"] = (
        None
        if row["estimated_cost_savings_usd"] is None
        else float(row["estimated_cost_savings_usd"])
    )
    row["finding"] = payload
    return row


def _report_cost_completeness(report: dict[str, Any]) -> tuple[str, int, int]:
    """Return validated status and priced/unavailable call counts for storage."""

    breakdown = report.get("cost_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    total, count_is_valid = _normalized_model_call_count(report)
    if not count_is_valid:
        return "unknown", 0, 0
    if _normalized_estimated_cost(report) is None and total:
        return "unknown", 0, total
    status = report.get("cost_status")
    status = status if status in _COST_STATUSES else None

    try:
        priced = max(0, int(breakdown.get("priced_model_call_count", 0) or 0))
        unavailable = max(0, int(breakdown.get("unavailable_model_call_count", 0) or 0))
    except (OverflowError, TypeError, ValueError):
        priced = unavailable = 0

    if priced + unavailable != total:
        if status == "unknown":
            priced, unavailable = 0, total
        elif status == "partial" and total > 1:
            priced, unavailable = total - 1, 1
        elif status == "complete":
            priced, unavailable = total, 0
        else:
            priced, unavailable = 0, total
    return _aggregate_cost_status(priced, unavailable), priced, unavailable


def _normalized_model_call_count(report: dict[str, Any]) -> tuple[int, bool]:
    raw = report.get("model_call_count", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0, False
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        return 0, False
    count = int(raw)
    return (count, True) if 0 <= count <= _MAX_DATABASE_INTEGER else (0, False)


def _normalized_estimated_cost(report: dict[str, Any]) -> float | None:
    raw = report.get("estimated_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        cost = float(raw)
    except OverflowError:
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _aggregate_cost_status(priced: int, unavailable: int, *, incomplete_run_count: int = 0) -> str:
    if incomplete_run_count:
        return "unknown" if priced == 0 else "partial"
    total = priced + unavailable
    if total == 0:
        return "empty"
    if unavailable == 0:
        return "complete"
    if priced == 0:
        return "unknown"
    return "partial"


def _merge_cost_status(left: str | None, right: str) -> str:
    if left is None or left == "empty" or left == right:
        return right
    if right == "empty":
        return left
    return "partial"


def _max_severity(left: str, right: str) -> str:
    rank = {"high": 3, "medium": 2, "low": 1}
    return left if rank.get(left, 0) >= rank.get(right, 0) else right


def _build_optimization_queue(
    findings: list[dict[str, Any]], project_id: str | None
) -> list[dict[str, Any]]:
    """Cluster unresolved findings into a priority-ranked optimization queue.

    Within one run, findings of the same type and title that share affected
    spans are the same underlying problem reported per occurrence — summing
    their savings would double-count. Each cluster's savings therefore come
    from the compatible (span-disjoint) selection per run, summed across runs,
    and ranking retains the same compatible-span accounting.
    """
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    # A scope is (project_id, run_id); project_id may be None.
    cluster_items: dict[tuple[str, str], dict[tuple[Any, Any], list[SavingsItem]]] = {}
    members: dict[tuple[str, str], list[dict[str, Any]]] = {}
    run_scopes: dict[tuple[str, str], set[tuple[Any, Any]]] = {}
    run_ids: dict[tuple[str, str], set[Any]] = {}
    for finding in findings:
        key = (finding["type"], finding["title"])
        members.setdefault(key, []).append(finding)
        cluster = clusters.setdefault(
            key,
            {
                "queue_id": "queue_"
                + hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:12],
                "type": finding["type"],
                "title": finding["title"],
                "status": "detected",
                "severity": finding["severity"],
                "occurrence_count": 0,
                "run_count": 0,
                "affected_runs": [],
                "affected_run_refs": [],
                "patchable_count": 0,
                "estimated_latency_savings_ms": 0.0,
                "estimated_cost_savings_usd": 0.0,
                "cost_status": None,
                "latest_created_at": finding["created_at"],
                "project_id": project_id,
                "quality_risk": "unknown",
                "requires_scorer": True,
                "safe_to_auto_patch": False,
            },
        )
        cluster["occurrence_count"] += 1
        if finding["estimated_latency_savings_ms"] is None:
            cluster["unmodeled_latency_count"] = cluster.get("unmodeled_latency_count", 0) + 1
        scope = (finding.get("project_id"), finding["run_id"])
        if scope not in run_scopes.setdefault(key, set()):
            run_scopes[key].add(scope)
            cluster["affected_run_refs"].append({"project_id": scope[0], "run_id": scope[1]})
            cluster["run_count"] += 1
        if finding["run_id"] not in run_ids.setdefault(key, set()):
            run_ids[key].add(finding["run_id"])
            cluster["affected_runs"].append(finding["run_id"])
        cluster["patchable_count"] += 1 if finding["patchable"] else 0
        cluster["severity"] = _max_severity(cluster["severity"], finding["severity"])
        cluster["latest_created_at"] = max(
            str(cluster["latest_created_at"]), str(finding["created_at"])
        )
        raw_payload = finding.get("finding")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        metadata = payload.get("metadata")
        finding_status = metadata.get("cost_status") if isinstance(metadata, dict) else None
        if finding_status not in _COST_STATUSES:
            finding_status = (
                "complete" if finding["estimated_cost_savings_usd"] is not None else "unknown"
            )
        cluster["cost_status"] = _merge_cost_status(cluster["cost_status"], finding_status)
        cluster_items.setdefault(key, {}).setdefault(scope, []).append(
            SavingsItem(
                spans=frozenset(payload.get("affected_spans") or []),
                latency_ms=float(finding["estimated_latency_savings_ms"] or 0.0),
                cost_usd=float(finding["estimated_cost_savings_usd"] or 0.0),
            )
        )

    selection_cache: dict[
        tuple[str, str], dict[tuple[Any, Any], tuple[list[SavingsItem], Selection]]
    ] = {}
    for key, cluster in clusters.items():
        cluster["affected_runs"].sort()
        cluster["affected_run_refs"].sort(
            key=lambda value: (str(value["project_id"]), value["run_id"])
        )
        latency = 0.0
        cost = 0.0
        for scope, run_items in cluster_items[key].items():
            selection = select_compatible(run_items)
            selection_cache.setdefault(key, {})[scope] = (run_items, selection)
            latency += selection.latency_ms
            cost += selection.cost_usd
        cluster["estimated_latency_savings_ms"] = (
            None if cluster.get("unmodeled_latency_count") else round(latency, 3)
        )
        cluster["cost_status"] = cluster["cost_status"] or "complete"
        cluster["estimated_cost_savings_usd"] = (
            round(cost, 6) if cluster["cost_status"] in {"complete", "empty"} else None
        )

    queue = list(clusters.values())
    for item in queue:
        key = (item["type"], item["title"])
        item["ranking"] = cluster_ranking(members[key], selection_cache=selection_cache[key])
        item["quality_risk"] = item["ranking"]["components"]["quality_risk"]["value"] or "unknown"
        item["requires_scorer"] = True
        item["safe_to_auto_patch"] = False
    return finalize_ranking(queue)
