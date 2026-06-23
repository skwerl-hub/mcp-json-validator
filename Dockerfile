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
# MCP stdio servers communicate over stdin/stdout — no TCP port needed.
# These ENV vars are overridden at deploy time via Railway/Fly secrets.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GEMINI_API_KEY="" \
    MCP_API_KEYS=""

# Health check: verify the Python environment is intact
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import mcp, google.generativeai, jsonschema, pydantic; print('ok')" || exit 1

# MCP servers speak JSON-RPC over stdio — invoke directly
ENTRYPOINT ["python", "server.py"]
