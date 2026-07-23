# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Single worker keeps the in-memory knowledge base + feedback log consistent
# for the demo. Serves the MCP streamable-http endpoint at /mcp and the
# unauthenticated /health probe on port 8080.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
