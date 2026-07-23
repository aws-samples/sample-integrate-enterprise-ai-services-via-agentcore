# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The Claude Agent SDK drives the Claude Code CLI as a subprocess, which
# requires Node.js. Install Node 20 (LTS) and the CLI globally so the SDK can
# spawn it at runtime. Bedrock routing is configured via env vars at deploy
# time (CLAUDE_CODE_USE_BEDROCK=1, ANTHROPIC_MODEL, AWS_REGION).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Single worker keeps the in-memory conversation store consistent for the demo.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
