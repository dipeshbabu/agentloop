from __future__ import annotations

import os

_POSTGRES_URL_ENV_VARS = ("AGENTLOOP_DATABASE_URL", "DATABASE_URL")
_POSTGRES_LIBPQ_SOURCE_ENV_VARS = ("PGHOST", "PGHOSTADDR", "PGSERVICE")


def get_api_key() -> str | None:
    return os.getenv("AGENTLOOP_API_KEY")


def get_admin_api_key() -> str | None:
    return os.getenv("AGENTLOOP_ADMIN_API_KEY")


def require_api_key() -> bool:
    return os.getenv("AGENTLOOP_REQUIRE_API_KEY", "false").lower() in {"1", "true", "yes", "on"}


def get_api_url() -> str:
    return os.getenv("AGENTLOOP_API_URL", "http://127.0.0.1:8000")


def get_cors_origins() -> list[str]:
    value = os.getenv("AGENTLOOP_CORS_ORIGINS", "")
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def get_postgres_dsn() -> str | None:
    """Return the explicit Postgres DSN, preserving AgentLoop override precedence."""
    for name in _POSTGRES_URL_ENV_VARS:
        value = os.getenv(name)
        if value:
            return value
    return None


def get_postgres_password_file() -> str | None:
    """Return the optional file containing a libpq password."""
    return os.getenv("AGENTLOOP_POSTGRES_PASSWORD_FILE") or None


def postgres_connection_source() -> str | None:
    """Describe the configured Postgres connection source without exposing secrets."""
    if get_postgres_dsn():
        return "database URL"
    if any(os.getenv(name) for name in _POSTGRES_LIBPQ_SOURCE_ENV_VARS):
        return "libpq environment"
    return None
