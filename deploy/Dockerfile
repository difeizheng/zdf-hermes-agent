FROM python:3.11-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml uv.lock* ./
RUN pip install uv && \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev; \
    else \
        uv pip install -e ".[all]"; \
    fi

FROM base AS app

COPY . .

RUN mkdir -p /data/hermes/{logs,pids,tasks}
ENV HERMES_HOME=/data/hermes
ENV PYTHONUNBUFFERED=1

EXPOSE 9100
