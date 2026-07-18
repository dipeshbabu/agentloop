from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TRUE_VALUES = {"1", "true", "yes", "on"}


class _ClearSentinel:
    """Sentinel passed to :func:`init` to explicitly clear an optional value.

    ``init(api_key=None)`` means "keep the current value" (so environment and
    prior configuration survive a reconfigure). Passing ``CLEAR`` instead sets
    the value back to ``None``.
    """

    def __repr__(self) -> str:
        return "CLEAR"


CLEAR = _ClearSentinel()


class FinalizationError(RuntimeError):
    """Raised by :func:`finalize_trace` when a destination fails and
    ``fail_silently`` is ``False``.

    ``result`` holds the partial finalization result for destinations that
    completed before the failure, so already-completed work is not lost.
    ``errors`` lists the destination-tagged failures.
    """

    def __init__(
        self,
        message: str,
        *,
        result: dict[str, Any],
        errors: list[dict[str, str]],
    ) -> None:
        super().__init__(message)
        self.result = result
        self.errors = errors


@dataclass
class AgentLoopRuntimeConfig:
    api_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    project_id: str = "default"
    auto_upload: bool = False
    auto_store: bool = False
    fail_silently: bool = True
    export_dir: Path | None = None


_runtime = AgentLoopRuntimeConfig()
_last_error: str | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in _TRUE_VALUES


def init(
    *,
    api_url: str | None = None,
    api_key: str | _ClearSentinel | None = None,
    project_id: str | None = None,
    auto_upload: bool | None = None,
    auto_store: bool | None = None,
    fail_silently: bool | None = None,
    export_dir: str | Path | _ClearSentinel | None = None,
) -> AgentLoopRuntimeConfig:
    """Configure AgentLoop once at process startup.

    Examples:

    ```python
    import agentloop

    agentloop.init(api_key="al_xxx", project_id="demo", auto_upload=True)
    ```

    Environment variables are also supported:
    - `AGENTLOOP_API_URL`
    - `AGENTLOOP_API_KEY`
    - `AGENTLOOP_PROJECT_ID`
    - `AGENTLOOP_AUTO_UPLOAD`
    - `AGENTLOOP_AUTO_STORE`
    - `AGENTLOOP_FAIL_SILENTLY`
    - `AGENTLOOP_EXPORT_DIR`

    Optional values (`api_key`, `export_dir`) treat ``None`` as "keep the current
    value". Pass :data:`CLEAR` to reset one of them back to ``None``.
    """

    global _runtime

    if api_key is CLEAR:
        resolved_api_key: str | None = None
    elif api_key is not None:
        resolved_api_key = api_key  # type: ignore[assignment]
    else:
        resolved_api_key = os.getenv("AGENTLOOP_API_KEY", _runtime.api_key)

    if export_dir is CLEAR:
        resolved_export_dir: Path | None = None
    elif export_dir is not None:
        resolved_export_dir = Path(export_dir)  # type: ignore[arg-type]
    else:
        env_export_dir = os.getenv("AGENTLOOP_EXPORT_DIR")
        resolved_export_dir = Path(env_export_dir) if env_export_dir else _runtime.export_dir

    _runtime = AgentLoopRuntimeConfig(
        api_url=api_url or os.getenv("AGENTLOOP_API_URL", _runtime.api_url),
        api_key=resolved_api_key,
        project_id=project_id or os.getenv("AGENTLOOP_PROJECT_ID", _runtime.project_id),
        auto_upload=auto_upload
        if auto_upload is not None
        else _env_bool("AGENTLOOP_AUTO_UPLOAD", _runtime.auto_upload),
        auto_store=auto_store
        if auto_store is not None
        else _env_bool("AGENTLOOP_AUTO_STORE", _runtime.auto_store),
        fail_silently=fail_silently
        if fail_silently is not None
        else _env_bool("AGENTLOOP_FAIL_SILENTLY", _runtime.fail_silently),
        export_dir=resolved_export_dir,
    )
    return _runtime


def configure_from_env() -> AgentLoopRuntimeConfig:
    return init()


def get_runtime_config() -> AgentLoopRuntimeConfig:
    return _runtime


def get_last_error() -> str | None:
    return _last_error


def reset_runtime() -> None:
    global _runtime, _last_error
    _runtime = AgentLoopRuntimeConfig()
    _last_error = None


def should_auto_export() -> bool:
    return bool(_runtime.auto_upload or _runtime.auto_store or _runtime.export_dir)


def finalize_trace(trace: Any) -> dict[str, Any]:
    """Apply configured end-of-run side effects.

    Depending on runtime config, this exports a JSON file, saves to the local
    persistent store, and/or uploads to the configured API. Each destination runs
    in its own error boundary in a deterministic order (export, store, upload), so
    a failure in one cannot prevent the others from running.

    When ``fail_silently`` is ``True`` (the default), every configured destination
    is attempted and failures are collected in ``result["errors"]``, each tagged
    with its ``destination``. When ``fail_silently`` is ``False``, the first failing
    destination raises :class:`FinalizationError` (fail-fast); its ``result``
    attribute still carries the destinations that completed before the failure.

    A finalization in which every attempted destination succeeds clears any stale
    process-global error so :func:`get_last_error` reflects the latest outcome.
    """

    global _last_error
    result: dict[str, Any] = {
        "exported_path": None,
        "stored": False,
        "uploaded": False,
        "errors": [],
    }

    def _do_export() -> None:
        export_dir = _runtime.export_dir
        if export_dir is None:  # pragma: no cover - guarded before registration
            return
        export_dir.mkdir(parents=True, exist_ok=True)
        out = export_dir / f"{trace.run_id}.json"
        trace.export_json(out)
        result["exported_path"] = str(out)

    def _do_store() -> None:
        from agentloop.store import get_store

        db = get_store()
        db.init()
        db.save_trace(trace, project_id=_runtime.project_id)
        result["stored"] = True

    def _do_upload() -> None:
        from agentloop.client import AgentLoopClient

        client = AgentLoopClient(base_url=_runtime.api_url, api_key=_runtime.api_key)
        result["upload_response"] = client.upload_trace(trace)
        result["uploaded"] = True

    destinations: list[tuple[str, Any]] = []
    if _runtime.export_dir is not None:
        destinations.append(("export", _do_export))
    if _runtime.auto_store:
        destinations.append(("store", _do_store))
    if _runtime.auto_upload:
        destinations.append(("upload", _do_upload))

    errors: list[dict[str, str]] = []
    for destination, action in destinations:
        try:
            action()
        except Exception as exc:
            errors.append({"destination": destination, "error": str(exc)})
            if not _runtime.fail_silently:
                result["errors"] = errors
                _last_error = f"{destination}: {exc}"
                raise FinalizationError(
                    f"{destination} finalization failed: {exc}",
                    result=result,
                    errors=errors,
                ) from exc

    result["errors"] = errors
    if errors:
        last = errors[-1]
        _last_error = f"{last['destination']}: {last['error']}"
    elif destinations:
        # Every attempted destination succeeded; drop any stale error state.
        _last_error = None

    return result


# Load env-driven defaults on import without forcing users to call init().
configure_from_env()
