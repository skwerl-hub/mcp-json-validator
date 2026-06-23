# ============================================================
# Self-Healing JSON Validator — MCP Server
# Multi-stage build optimised for Railway / Fly.io
# ============================================================

# ── Stage 1: dependency builder ──────────────────────────────
FROM python:3.12-slim AS builder

# System deps for compiling any C-extension wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the lockfile first to maximise layer cache hits
COPY requirements.txt .

# Install into an isolated prefix so we can copy cleanly
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ───────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: run as non-root
RUN groupadd --gid 1001 mcpuser \
    && useradd --uid 1001 --gid mcpuser --shell /bin/sh --create-home mcpuser

# Pull installed packages from the builder stage
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source
COPY server.py .

# Transfer ownership so the non-root user can read the files
RUN chown -R mcpuser:mcpuser /app

USER mcpuser

# ── Runtime configuration ─────────────────────────────────────
# PORT is injected automatically by Railway.
# These ENV vars are overridden at deploy time via Railway/Fly secrets.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    GEMINI_API_KEY="" \
    MCP_API_KEYS=""

EXPOSE 8000

# Health check: hit the /health HTTP endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start the SSE web server
ENTRYPOINT ["python", "server.py"]
