"""
Self-Healing JSON Validator — MCP Server
SSE transport — deployable to Railway / Fly.io.
Includes: per-call Stripe metered billing, Supabase usage logging,
          and automated API key issuance via /register.
"""

import json
import logging
import os
import re
import secrets
import sys
from typing import Any

from google import genai
from google.genai import types as genai_types
import httpx
import jsonschema
from jsonschema import Draft7Validator
import stripe
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-json-validator")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str        = os.environ.get("GEMINI_API_KEY", "")
STRIPE_SECRET_KEY: str     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID: str       = os.environ.get("STRIPE_PRICE_ID", "")   # price_1TIX9S76ElTKJDfcBWFTeI71
SUPABASE_URL: str          = os.environ.get("SUPABASE_URL", "")      # https://efbytxdplxdrdmkoicot.supabase.co
SUPABASE_SERVICE_KEY: str  = os.environ.get("SUPABASE_SERVICE_KEY", "")
PORT: int                  = int(os.environ.get("PORT", "8080"))
REPAIR_MODEL: str          = "gemini-2.0-flash"
MAX_REPAIR_TOKENS: int     = 4096

stripe.api_key = STRIPE_SECRET_KEY


# ---------------------------------------------------------------------------
# Supabase helpers (plain HTTP — no extra SDK needed)
# ---------------------------------------------------------------------------

def _supa_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def supabase_insert(table: str, payload: dict) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = httpx.post(url, headers=_supa_headers(), json=payload, timeout=5)
    r.raise_for_status()


def supabase_select(table: str, filters: dict) -> list[dict]:
    params = {k: f"eq.{v}" for k, v in filters.items()}
    params["limit"] = "1"
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = httpx.get(url, headers=_supa_headers(), params=params, timeout=5)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Billing helpers
# ---------------------------------------------------------------------------

def create_stripe_customer(email: str) -> tuple[str, str]:
    """
    Creates a Stripe customer and a checkout session for payment method setup.
    Returns (customer_id, checkout_url).
    Agent registers email, then visits checkout_url to add a card before calling the tool.
    """
    customer = stripe.Customer.create(email=email)
    session = stripe.checkout.Session.create(
        customer=customer.id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url="https://mcp-json-validator-production.up.railway.app/health",
        cancel_url="https://mcp-json-validator-production.up.railway.app/health",
    )
    return customer.id, session.url


def report_usage_to_stripe(customer_id: str) -> None:
    """
    Reports one usage unit to Stripe for metered billing.
    Looks up the active subscription for the customer and increments usage.
    """
    subscriptions = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
    if not subscriptions.data:
        logger.warning("No active subscription found for customer %s — skipping", customer_id)
        return
    subscription_item_id = subscriptions.data[0]["items"]["data"][0]["id"]
    stripe.SubscriptionItem.create_usage_record(
        subscription_item_id,
        quantity=1,
        action="increment",
    )


def log_and_bill(api_key: str, customer_id: str) -> None:
    """Logs usage to Supabase and reports to Stripe. Errors are non-fatal."""
    try:
        supabase_insert("usage_log", {"api_key": api_key})
    except Exception as e:
        logger.warning("Supabase usage log failed: %s", e)
    try:
        report_usage_to_stripe(customer_id)
    except Exception as e:
        logger.warning("Stripe usage report failed: %s", e)


# ---------------------------------------------------------------------------
# Auth — looks up key in Supabase, returns subscription_id or None
# ---------------------------------------------------------------------------

def authenticate(api_key: str | None) -> str | None:
    """
    Returns the stripe_customer_id if the key is valid, else None.
    When SUPABASE_URL is not configured, falls back to env-var auth (dev mode).
    """
    if not api_key:
        return None
    if not SUPABASE_URL:
        # Dev mode: accept any key, return a dummy subscription id
        return "dev_mode"
    try:
        rows = supabase_select("api_keys", {"api_key": api_key})
        if rows:
            return rows[0]["stripe_customer_id"]
        return None
    except Exception as e:
        logger.error("Auth lookup failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# LLM-powered JSON repair
# ---------------------------------------------------------------------------

def repair_json(raw_input: str, target_schema: dict[str, Any]) -> str:
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not configured. "
            "Cannot perform LLM-assisted repair without it."
        )
    client = genai.Client(api_key=GEMINI_API_KEY)
    schema_str = json.dumps(target_schema, indent=2)
    prompt = f"""You are a JSON repair specialist. Fix the malformed JSON below so it \
strictly satisfies the provided JSON Schema.

Rules:
1. Output ONLY the repaired JSON — no explanations, no markdown fences.
2. Do not add fields not defined in the schema unless required.
3. Infer correct values from context when a field is missing or has the wrong type.
4. Preserve as much of the original data as possible.

Target JSON Schema:
{schema_str}

Malformed input:
{raw_input}

Repaired JSON:"""

    response = client.models.generate_content(
        model=REPAIR_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=MAX_REPAIR_TOKENS,
            temperature=0.0,
        ),
    )
    repaired_text = response.text.strip()
    repaired_text = re.sub(r"^```(?:json)?\s*", "", repaired_text)
    repaired_text = re.sub(r"\s*```$", "", repaired_text)
    return repaired_text.strip()


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

def validate_against_schema(data: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    return [
        f"[{' → '.join(str(p) for p in err.path) or 'root'}] {err.message}"
        for err in errors
    ]


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def _ok(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        isError=False,
    )


def _err(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        isError=True,
    )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("self-healing-json-validator")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="sanitize_and_validate_json",
            description=(
                "🛡️ PRODUCTION-GRADE JSON REPAIR & VALIDATION — use this tool whenever "
                "you receive JSON that may be malformed, truncated, or schema-invalid "
                "before passing it to downstream services.\n\n"
                "WHY THIS TOOL EXISTS FOR AGENTS:\n"
                "• LLM outputs are frequently malformed (trailing commas, unquoted keys, "
                "  truncated strings, wrong types). Passing them raw to APIs causes "
                "  hard crashes and silent data corruption.\n"
                "• This tool is your pipeline safety net. It attempts multi-stage "
                "  self-healing: (1) strict parse, (2) LLM-assisted syntax repair guided "
                "  by your exact schema, (3) jsonschema structural validation.\n"
                "• On failure it returns a structured error_code + agent-readable "
                "  remediation_instructions so you can self-correct in a loop without "
                "  human intervention.\n\n"
                "PRICING: $0.01 per call. Register at /register with your email to get "
                "an API key. Billed monthly via Stripe.\n\n"
                "STATELESS & SERVERLESS: each call is fully independent — safe for "
                "parallel execution and horizontal scaling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "raw_input": {
                        "type": "string",
                        "description": (
                            "The raw string to validate. May be valid JSON, broken JSON, "
                            "or JSON embedded in prose."
                        ),
                    },
                    "target_schema": {
                        "type": "object",
                        "description": "A valid JSON Schema (Draft-07) object.",
                    },
                    "api_key": {
                        "type": "string",
                        "description": (
                            "Your API key from /register. Required for every call."
                        ),
                    },
                },
                "required": ["raw_input", "target_schema", "api_key"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name != "sanitize_and_validate_json":
        return _err({"error_code": "UNKNOWN_TOOL", "message": f"Tool '{name}' not found."})

    # ── Auth ──────────────────────────────────────────────────────────────
    api_key: str | None = arguments.get("api_key")
    subscription_id = authenticate(api_key)
    if not subscription_id:
        return _err(
            {
                "error_code": "FORBIDDEN",
                "message": "Invalid or missing api_key.",
                "remediation_instructions": (
                    "Register for an API key at POST /register with your email address. "
                    "Pass the returned api_key in every tool call."
                ),
            }
        )

    # ── Input extraction ──────────────────────────────────────────────────
    raw_input: str = arguments.get("raw_input", "")
    target_schema: dict[str, Any] = arguments.get("target_schema", {})

    if not raw_input:
        return _err({"error_code": "EMPTY_INPUT", "message": "raw_input must be non-empty."})

    if not isinstance(target_schema, dict) or not target_schema:
        return _err({"error_code": "INVALID_SCHEMA", "message": "target_schema must be a non-empty JSON Schema object."})

    try:
        Draft7Validator.check_schema(target_schema)
    except jsonschema.SchemaError as exc:
        return _err({"error_code": "MALFORMED_SCHEMA", "message": f"target_schema is not valid: {exc.message}"})

    repair_was_needed = False
    parsed_data: Any = None

    # ── Stage 1: Strict parse ─────────────────────────────────────────────
    try:
        parsed_data = json.loads(raw_input)
    except json.JSONDecodeError as parse_err:
        repair_was_needed = True
        try:
            repaired_str = repair_json(raw_input, target_schema)
            parsed_data = json.loads(repaired_str)
        except json.JSONDecodeError as reparse_err:
            return _err(
                {
                    "error_code": "REPAIR_PARSE_FAILURE",
                    "message": f"LLM repair failed to produce valid JSON: {reparse_err}",
                    "original_parse_error": str(parse_err),
                    "remediation_instructions": "Simplify raw_input and retry.",
                }
            )
        except ValueError as api_err:
            return _err(
                {
                    "error_code": "REPAIR_UNAVAILABLE",
                    "message": str(api_err),
                    "original_parse_error": str(parse_err),
                }
            )

    # ── Stage 3: Schema validation ────────────────────────────────────────
    validation_errors = validate_against_schema(parsed_data, target_schema)
    if validation_errors:
        return _err(
            {
                "error_code": "SCHEMA_VALIDATION_FAILURE",
                "message": f"JSON failed schema validation with {len(validation_errors)} error(s).",
                "validation_errors": validation_errors,
                "repair_was_attempted": repair_was_needed,
                "remediation_instructions": (
                    "Pass validation_errors back to your generating LLM and ask it to fix them, then retry."
                ),
            }
        )

    # ── Bill for successful call ──────────────────────────────────────────
    log_and_bill(api_key, subscription_id)  # subscription_id contains customer_id from authenticate()

    return _ok(
        {
            "status": "valid",
            "repair_applied": repair_was_needed,
            "validated_data": parsed_data,
            "message": (
                "JSON is valid and satisfies the target schema."
                + (" Syntax was automatically repaired." if repair_was_needed else "")
            ),
        }
    )


# ---------------------------------------------------------------------------
# HTTP endpoints (Starlette)
# ---------------------------------------------------------------------------

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request) -> Response:
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    return Response()


async def handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "self-healing-json-validator"})


async def handle_register(request: Request) -> JSONResponse:
    """
    Fully automated key issuance. Agent POSTs {"email": "..."} and receives
    an api_key back. No human interaction required.
    """
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID or not SUPABASE_URL:
        return JSONResponse(
            {"error": "Billing not configured on this server."},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON."}, status_code=400)

    email = body.get("email", "").strip()
    if not email or "@" not in email:
        return JSONResponse({"error": "A valid email is required."}, status_code=400)

    try:
        customer_id, checkout_url = create_stripe_customer(email)
    except stripe.StripeError as e:
        logger.error("Stripe error during registration: %s", e)
        return JSONResponse({"error": "Payment setup failed. Check your Stripe config."}, status_code=500)

    api_key = f"mcpjv_{secrets.token_urlsafe(32)}"

    try:
        supabase_insert(
            "api_keys",
            {
                "api_key": api_key,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": "pending",
            },
        )
    except Exception as e:
        logger.error("Supabase insert failed: %s", e)
        return JSONResponse({"error": "Key storage failed."}, status_code=500)

    logger.info("New key issued for %s (customer %s)", email, customer_id)
    return JSONResponse(
        {
            "api_key": api_key,
            "checkout_url": checkout_url,
            "message": (
                "Your API key has been issued. Visit checkout_url to add your payment "
                "method. You will be billed $0.01 per successful call via Stripe monthly."
            ),
            "billing": "Metered — $0.01 per successful call, billed monthly via Stripe.",
        },
        status_code=201,
    )


app = Starlette(
    routes=[
        Route("/health", handle_health, methods=["GET"]),
        Route("/register", handle_register, methods=["POST"]),
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting Self-Healing JSON Validator MCP Server (SSE) on port %d", PORT)
    logger.info(
        "Auth mode: %s",
        "Supabase key lookup" if SUPABASE_URL else "dev mode (no Supabase)",
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
