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
    STRIPE_SECRET_KEY="" \
    STRIPE_PRICE_ID="" \
    SUPABASE_URL="" \
    SUPABASE_SERVICE_KEY=""

EXPOSE 8080

# Let Railway handle health checks via its HTTP checker on /health
# No HEALTHCHECK instruction — Railway will detect the port automatically

ENTRYPOINT ["python", "server.py"]
