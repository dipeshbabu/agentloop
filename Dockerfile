FROM python:3.15-slim

LABEL org.opencontainers.image.title="AgentLoop" \
      org.opencontainers.image.description="AgentLoop API and dashboard for agent traces and reports" \
      org.opencontainers.image.source="https://github.com/dipeshbabu/agentloop" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTLOOP_SQLITE_PATH=/data/agentloop.db \
    AGENTLOOP_DASHBOARD_RUNS_DIR=/data/runs \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" agentloop

COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_LICENSES.md ./
COPY agentloop ./agentloop
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit
COPY scripts ./scripts

RUN uv sync --frozen --no-dev --all-extras --no-editable

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /data && chown -R agentloop:agentloop /data

USER agentloop

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "agentloop.server:app", "--host", "0.0.0.0", "--port", "8000"]
