# Self-Healing JSON Validator — MCP Server

Production-grade JSON repair and validation for agent pipelines. Automatically fixes malformed LLM output using Gemini, then validates against your JSON Schema. Built for agents that need data integrity without human intervention.

**$0.025 per successful call. Fully automated. No humans involved.**

---

## Why This Exists

LLMs generate broken JSON constantly — trailing commas, unquoted keys, truncated strings, wrong types. Passing that output raw to an API or database causes hard crashes and silent data corruption. This tool is the safety net between your generator and your consumers.

## How It Works

Every call runs through three stages:

```
raw_input
    │
    ▼
Stage 1: Strict JSON parse      → PASS → Stage 3
    │ FAIL
    ▼
Stage 2: LLM repair (Gemini)    → re-parse → PASS → Stage 3
    │ FAIL
    ▼
REPAIR_PARSE_FAILURE error with remediation instructions
    
Stage 3: JSON Schema validation → PASS → ✅ validated_data returned
    │ FAIL
    ▼
SCHEMA_VALIDATION_FAILURE error with per-field error list
```

## Getting Started

### 1. Register for an API key

```bash
curl -X POST https://mcp-json-validator-production.up.railway.app/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Returns:
```json
{
  "api_key": "mcpjv_...",
  "checkout_url": "https://checkout.stripe.com/...",
  "billing": "Metered — $0.025 per successful call, billed monthly via Stripe."
}
```

Visit the `checkout_url` to add your payment method. Your key works immediately.

### 2. Connect to the MCP server

```
https://mcp-json-validator-production.up.railway.app/sse
```

### 3. Call the tool

```json
{
  "tool": "sanitize_and_validate_json",
  "arguments": {
    "api_key": "mcpjv_your_key_here",
    "raw_input": "{'name': 'Alice', 'age': '30', scores: [95, 87,]}",
    "target_schema": {
      "type": "object",
      "required": ["name", "age", "scores"],
      "properties": {
        "name":   { "type": "string" },
        "age":    { "type": "integer" },
        "scores": { "type": "array", "items": { "type": "number" } }
      }
    }
  }
}
```

Response:
```json
{
  "status": "valid",
  "repair_applied": true,
  "validated_data": { "name": "Alice", "age": 30, "scores": [95, 87] },
  "message": "JSON is valid and satisfies the target schema. Syntax was automatically repaired."
}
```

## Error Codes

| Code | Meaning | What to do |
|---|---|---|
| `FORBIDDEN` | Invalid or missing api_key | Register at `/register` |
| `EMPTY_INPUT` | raw_input was empty | Check upstream step produced output |
| `INVALID_SCHEMA` | target_schema missing or not an object | Fix schema definition |
| `MALFORMED_SCHEMA` | target_schema fails JSON Schema meta-validation | Check for typos in schema keywords |
| `REPAIR_PARSE_FAILURE` | LLM repair failed to produce valid JSON | Regenerate raw_input from scratch |
| `REPAIR_UNAVAILABLE` | Repair stage unavailable | Check server status |
| `SCHEMA_VALIDATION_FAILURE` | JSON valid but structure wrong | Read validation_errors, fix and retry |

## Self-Healing Loop Pattern

When you receive `SCHEMA_VALIDATION_FAILURE`, feed the errors back to your generator:

```
Your previous output had these schema violations:
<validation_errors from tool response>

Regenerate the JSON correcting every violation listed above.
```

Then call the tool again. Limit retries to 3 before escalating.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/sse` | GET | MCP connection point |
| `/register` | POST | Get an API key |
| `/health` | GET | Server health check |
| `/.well-known/mcp/server-card.json` | GET | Smithery discovery |

## Pricing

$0.025 per successful call, billed monthly via Stripe. You are only charged when the tool returns `status: valid`. Failed calls (auth errors, schema errors) are free.

## License

MIT
