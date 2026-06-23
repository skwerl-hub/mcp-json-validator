"""
Self-Healing JSON Validator — MCP Server
SSE transport — deployable to Railway / Fly.io.
"""

import json
import logging
import os
import re
import sys
from typing import Any

from google import genai
from google.genai import types as genai_types
import jsonschema
from jsonschema import Draft7Validator
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
from starlette.responses import Response
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
VALID_API_KEYS: set[str] = {
    k.strip()
    for k in os.environ.get("MCP_API_KEYS", "").split(",")
    if k.strip()
}
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
REPAIR_MODEL: str = "gemini-2.0-flash"  # cost-optimised; swap to gemini-1.5-pro if needed
MAX_REPAIR_TOKENS: int = 4096
PORT: int = int(os.environ.get("PORT", "8000"))  # Railway injects PORT automatically


# ---------------------------------------------------------------------------
# Auth helper (stateless — key is passed per-request in tool arguments)
# ---------------------------------------------------------------------------

def authenticate(api_key: str | None) -> bool:
    """
    Returns True when auth passes.
    • If VALID_API_KEYS env var is empty, auth is disabled (dev mode).
    • Otherwise the supplied key must appear in the set.
    """
    if not VALID_API_KEYS:
        return True  # auth not configured — open access
    return api_key in VALID_API_KEYS


# ---------------------------------------------------------------------------
# LLM-powered JSON repair
# ---------------------------------------------------------------------------

def repair_json(raw_input: str, target_schema: dict[str, Any]) -> str:
    """
    Calls Gemini to repair malformed JSON so it satisfies *target_schema*.
    Returns the repaired JSON string, or raises ValueError on failure.
    This function is intentionally stateless: every invocation is independent.
    """
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not configured. "
            "Cannot perform LLM-assisted repair without it."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)

    schema_str = json.dumps(target_schema, indent=2)
    prompt = f"""You are a JSON repair specialist. Your task is to fix the malformed or \
incomplete JSON below so that it strictly satisfies the provided JSON Schema.

Rules:
1. Output ONLY the repaired JSON — no explanations, no markdown fences.
2. Do not add fields not defined in the schema unless required.
3. Infer correct values from context when a field is missing or has the wrong type.
4. Preserve as much of the original data as possible.

Target JSON Schema:
{schema_str}

Malformed / broken input:
{raw_input}

Repaired JSON:"""

    response = client.models.generate_content(
        model=REPAIR_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=MAX_REPAIR_TOKENS,
            temperature=0.0,  # deterministic — we want JSON, not creativity
        ),
    )
    repaired_text: str = response.text.strip()

    # Strip accidental markdown fences the model may still emit
    repaired_text = re.sub(r"^```(?:json)?\s*", "", repaired_text)
    repaired_text = re.sub(r"\s*```$", "", repaired_text)

    return repaired_text.strip()


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

def validate_against_schema(
    data: Any, schema: dict[str, Any]
) -> list[str]:
    """
    Validates *data* against *schema* using jsonschema Draft7Validator.
    Returns a list of human-readable error messages (empty = valid).
    """
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
                "  by your exact schema, (3) Pydantic/jsonschema structural validation.\n"
                "• On failure it returns a structured error_code + agent-readable "
                "  remediation_instructions so you can self-correct in a loop without "
                "  human intervention.\n\n"
                "WHEN TO CALL THIS TOOL:\n"
                "• Before inserting agent-generated JSON into a database or calling an API.\n"
                "• After receiving tool/function-call outputs from another LLM.\n"
                "• Any time data integrity is required in a multi-agent pipeline.\n\n"
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
                            "or JSON embedded in prose. The tool will attempt to extract "
                            "and repair it automatically."
                        ),
                    },
                    "target_schema": {
                        "type": "object",
                        "description": (
                            "A valid JSON Schema (Draft-07) object describing the expected "
                            "structure. The repaired output MUST satisfy this schema to "
                            "receive a success response."
                        ),
                    },
                    "api_key": {
                        "type": "string",
                        "description": (
                            "X-API-Key for server authentication. Required when the server "
                            "is deployed with MCP_API_KEYS configured."
                        ),
                    },
                },
                "required": ["raw_input", "target_schema"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name != "sanitize_and_validate_json":
        return _err(
            {
                "error_code": "UNKNOWN_TOOL",
                "message": f"Tool '{name}' is not registered on this server.",
            }
        )

    # ── Auth ──────────────────────────────────────────────────────────────
    supplied_key: str | None = arguments.get("api_key")
    if not authenticate(supplied_key):
        logger.warning("Auth failure — invalid or missing api_key")
        return _err(
            {
                "error_code": "FORBIDDEN",
                "http_equivalent": 403,
                "message": (
                    "Authentication failed. Provide a valid 'api_key' argument matching "
                    "the server's MCP_API_KEYS configuration."
                ),
                "remediation_instructions": (
                    "1. Obtain a valid API key from the server operator.\n"
                    "2. Pass it as the 'api_key' field in your tool call arguments.\n"
                    "3. Do NOT embed it in raw_input or target_schema."
                ),
            }
        )

    # ── Input extraction ──────────────────────────────────────────────────
    raw_input: str = arguments.get("raw_input", "")
    target_schema: dict[str, Any] = arguments.get("target_schema", {})

    if not raw_input:
        return _err(
            {
                "error_code": "EMPTY_INPUT",
                "message": "raw_input must be a non-empty string.",
                "remediation_instructions": (
                    "Ensure the upstream step produces a non-empty string output "
                    "before calling this tool."
                ),
            }
        )

    if not isinstance(target_schema, dict) or not target_schema:
        return _err(
            {
                "error_code": "INVALID_SCHEMA",
                "message": "target_schema must be a non-empty JSON Schema object.",
                "remediation_instructions": (
                    "Provide a valid JSON Schema Draft-07 object. "
                    "At minimum: {\"type\": \"object\", \"properties\": {...}}"
                ),
            }
        )

    # Validate the schema itself is a legal JSON Schema
    try:
        Draft7Validator.check_schema(target_schema)
    except jsonschema.SchemaError as exc:
        return _err(
            {
                "error_code": "MALFORMED_SCHEMA",
                "message": f"target_schema is not a valid JSON Schema: {exc.message}",
                "remediation_instructions": (
                    "Fix the target_schema so it passes jsonschema Draft7 meta-validation "
                    "before retrying."
                ),
            }
        )

    repair_was_needed: bool = False
    parsed_data: Any = None

    # ── Stage 1: Strict parse ─────────────────────────────────────────────
    try:
        parsed_data = json.loads(raw_input)
        logger.info("Stage 1 PASS — raw_input is valid JSON")
    except json.JSONDecodeError as parse_err:
        logger.warning("Stage 1 FAIL — JSON parse error: %s", parse_err)

        # ── Stage 2: LLM repair ───────────────────────────────────────────
        repair_was_needed = True
        try:
            repaired_str = repair_json(raw_input, target_schema)
            logger.info("Stage 2: LLM repair produced output, re-parsing…")
            parsed_data = json.loads(repaired_str)
            logger.info("Stage 2 PASS — repaired JSON parses successfully")
        except json.JSONDecodeError as reparse_err:
            logger.error("Stage 2 FAIL — repaired output still invalid JSON: %s", reparse_err)
            return _err(
                {
                    "error_code": "REPAIR_PARSE_FAILURE",
                    "message": (
                        "LLM repair was attempted but the repaired output could not be "
                        f"parsed as JSON: {reparse_err}"
                    ),
                    "original_parse_error": str(parse_err),
                    "remediation_instructions": (
                        "1. Simplify raw_input — remove surrounding prose and provide only "
                        "   JSON-like content.\n"
                        "2. Verify target_schema is not excessively complex.\n"
                        "3. Check that GEMINI_API_KEY is valid and the repair model "
                        "   is reachable.\n"
                        "4. If input is fundamentally non-JSON, regenerate it from scratch."
                    ),
                }
            )
        except ValueError as api_err:
            logger.error("Stage 2 FAIL — repair call failed: %s", api_err)
            return _err(
                {
                    "error_code": "REPAIR_UNAVAILABLE",
                    "message": str(api_err),
                    "original_parse_error": str(parse_err),
                    "remediation_instructions": (
                        "Ensure GEMINI_API_KEY is set and valid on the server, "
                        "then retry. Alternatively, fix the JSON syntax manually before "
                        "submitting."
                    ),
                }
            )

    # ── Stage 3: Schema validation ────────────────────────────────────────
    validation_errors = validate_against_schema(parsed_data, target_schema)

    if validation_errors:
        logger.warning("Stage 3 FAIL — %d schema violation(s)", len(validation_errors))
        return _err(
            {
                "error_code": "SCHEMA_VALIDATION_FAILURE",
                "message": (
                    f"Parsed JSON failed schema validation with "
                    f"{len(validation_errors)} error(s)."
                ),
                "validation_errors": validation_errors,
                "repair_was_attempted": repair_was_needed,
                "remediation_instructions": (
                    "1. Review each validation_errors entry — it shows the JSON path "
                    "   and the constraint that was violated.\n"
                    "2. Either fix the upstream generator to produce conformant data, or "
                    "   update target_schema if the schema itself is wrong.\n"
                    "3. Re-submit with the corrected raw_input.\n"
                    "4. If you are in an agentic loop, pass validation_errors back to "
                    "   the generating LLM with instruction: 'Fix these schema violations "
                    "   and regenerate the JSON.'"
                ),
            }
        )

    # ── All stages passed ─────────────────────────────────────────────────
    logger.info("All validation stages PASSED%s", " (repair applied)" if repair_was_needed else "")
    return _ok(
        {
            "status": "valid",
            "repair_applied": repair_was_needed,
            "validated_data": parsed_data,
            "message": (
                "JSON is structurally valid and satisfies the target schema."
                + (" Syntax was automatically repaired by LLM." if repair_was_needed else "")
            ),
        }
    )


# ---------------------------------------------------------------------------
# SSE Web Application (Starlette)
# ---------------------------------------------------------------------------

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request) -> Response:
    """SSE endpoint — agents connect here to establish an MCP session."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    return Response()


async def handle_health(request: Request) -> Response:
    """Simple health check so Railway knows the container is alive."""
    return Response(
        content=json.dumps({"status": "ok", "server": "self-healing-json-validator"}),
        media_type="application/json",
    )


app = Starlette(
    routes=[
        Route("/", handle_health, methods=["GET"]),
        Route("/health", handle_health, methods=["GET"]),
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting Self-Healing JSON Validator MCP Server (SSE transport) on port %d", PORT
    )
    logger.info(
        "Auth mode: %s",
        f"enforced ({len(VALID_API_KEYS)} key(s))" if VALID_API_KEYS else "disabled (open)",
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
