FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTLOOP_SQLITE_PATH=/data/agentloop.db

WORKDIR /app

RUN adduser --disabled-password --gecos "" agentloop

COPY pyproject.toml README.md ./
COPY agentloop ./agentloop
COPY dashboard ./dashboard
COPY scripts ./scripts

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir ".[all]"

RUN mkdir -p /data && chown -R agentloop:agentloop /app /data

USER agentloop

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "agentloop.server:app", "--host", "0.0.0.0", "--port", "8000"]
