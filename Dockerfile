# ============================================================
# Self-Healing JSON Validator — MCP Server
# Multi-stage build optimised for Railway / Fly.io
# ============================================================

# ── Stage 1: dependency builder ──────────────────────────────
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ───────────────────────────────────
FROM python:3.12-slim AS runtime

RUN groupadd --gid 1001 mcpuser \
    && useradd --uid 1001 --gid mcpuser --shell /bin/sh --create-home mcpuser

COPY --from=builder /install /usr/local

WORKDIR /app

COPY server.py .

RUN chown -R mcpuser:mcpuser /app

USER mcpuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    GEMINI_API_KEY="" \
    MCP_API_KEYS=""

EXPOSE 8080

# Healthcheck uses $PORT so it always matches what uvicorn binds to
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT','8080') + '/health')" || exit 1

ENTRYPOINT ["python", "server.py"]
