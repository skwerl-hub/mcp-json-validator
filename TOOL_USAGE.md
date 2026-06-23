# TOOL_USAGE.md — Self-Healing JSON Validator
*Agent-readable capability specification. Version 1.0.*

---

## TL;DR for Agents

> **Call `sanitize_and_validate_json` before you pass any LLM-generated JSON to
> a database, API, or downstream agent.** It repairs broken syntax automatically,
> validates structure against your schema, and returns machine-readable error codes
> that let you self-correct without human intervention.

---

## Why This Tool Exists

Large language models generate JSON that is frequently broken in ways that are
invisible at a glance:

| Failure mode | Frequency | Impact |
|---|---|---|
| Trailing commas | Very common | `json.loads()` crash |
| Unquoted / single-quoted keys | Common | Silent parse failure |
| Truncated strings / arrays | Common on long outputs | Data loss |
| Wrong value types (`"123"` vs `123`) | Common | DB write rejected |
| Missing required fields | Common | API returns 422 |
| Extra fields blocked by `additionalProperties: false` | Occasional | Validation error |

Passing unfixed output to a production service causes hard crashes, corrupted
records, and silent data loss. This tool is the safety net between your generator
and your consumers.

---

## Tool Registration

```
Server name : self-healing-json-validator
Transport   : stdio (JSON-RPC 2.0)
Tool name   : sanitize_and_validate_json
```

---

## Input Schema

```json
{
  "type": "object",
  "required": ["raw_input", "target_schema"],
  "properties": {
    "raw_input": {
      "type": "string",
      "description": "Raw string to validate. May be valid JSON, broken JSON, or JSON embedded in prose."
    },
    "target_schema": {
      "type": "object",
      "description": "JSON Schema Draft-07 object. The repaired output must satisfy this schema."
    },
    "api_key": {
      "type": "string",
      "description": "Authentication key. Required when server is deployed with MCP_API_KEYS set."
    }
  }
}
```

---

## Processing Pipeline (what happens inside)

```
raw_input
    │
    ▼
┌─────────────────────────────┐
│  Stage 1: Strict JSON parse │  ──► PASS ──► Stage 3
└─────────────────────────────┘
    │ FAIL (SyntaxError)
    ▼
┌──────────────────────────────────────────────────────┐
│  Stage 2: LLM Repair                                 │
│  Claude receives raw_input + target_schema and       │
│  returns corrected JSON only (no prose).             │
│  Re-parsed — if still broken → REPAIR_PARSE_FAILURE  │
└──────────────────────────────────────────────────────┘
    │ PASS
    ▼
┌──────────────────────────────────────────────────────┐
│  Stage 3: Schema Validation (jsonschema Draft-07)    │
│  All fields, types, required keys, enum values,      │
│  pattern constraints checked exhaustively.           │
│  Failure → SCHEMA_VALIDATION_FAILURE + error list    │
└──────────────────────────────────────────────────────┘
    │ PASS
    ▼
 ✅ { "status": "valid", "validated_data": { … } }
```

---

## Success Response

```json
{
  "status": "valid",
  "repair_applied": false,
  "validated_data": { "...": "your parsed object here" },
  "message": "JSON is structurally valid and satisfies the target schema."
}
```

When `repair_applied` is `true`, the original input had syntax errors that were
automatically fixed. Treat `validated_data` as the canonical output — never use
the original `raw_input` downstream.

---

## Error Codes Reference

Use these codes in your control flow. Every error response includes
`remediation_instructions` — read them before deciding how to recover.

| `error_code` | Meaning | Recommended agent action |
|---|---|---|
| `FORBIDDEN` | Missing or invalid `api_key` | Obtain key from operator, retry with correct value |
| `EMPTY_INPUT` | `raw_input` was empty | Ensure upstream step produced output before calling |
| `INVALID_SCHEMA` | `target_schema` is missing or not an object | Fix schema definition in your prompt/config |
| `MALFORMED_SCHEMA` | `target_schema` fails JSON Schema meta-validation | Validate schema structure; check for typos in keyword names |
| `REPAIR_PARSE_FAILURE` | LLM repair ran but output still invalid JSON | Regenerate `raw_input` from scratch with tighter instructions |
| `REPAIR_UNAVAILABLE` | Repair stage skipped (no `ANTHROPIC_API_KEY`) | Contact operator OR fix syntax before submitting |
| `SCHEMA_VALIDATION_FAILURE` | JSON valid but structure wrong | Read `validation_errors`, regenerate with corrections |

---

## Self-Healing Loop Pattern

When you receive `SCHEMA_VALIDATION_FAILURE`, feed the error list back to the
generator LLM using this pattern:

```
System: You are a JSON generator. Output ONLY raw JSON, no prose.
User:   Generate a <target_object> matching this schema: <schema>

        Your previous output had these schema violations:
        <validation_errors from tool response>

        Regenerate the JSON correcting every violation listed above.
```

Then call `sanitize_and_validate_json` again with the new output. Limit your
retry loop to 3 iterations before escalating to a human operator.

---

## Example Call

```json
{
  "tool": "sanitize_and_validate_json",
  "arguments": {
    "api_key": "sk-your-key-here",
    "raw_input": "{'name': 'Alice', 'age': '30', scores: [95, 87,]}",
    "target_schema": {
      "type": "object",
      "required": ["name", "age", "scores"],
      "properties": {
        "name":   { "type": "string" },
        "age":    { "type": "integer", "minimum": 0 },
        "scores": { "type": "array", "items": { "type": "number" } }
      },
      "additionalProperties": false
    }
  }
}
```

**What happens:** Stage 1 fails (single quotes, trailing comma). Stage 2
repairs to `{"name": "Alice", "age": 30, "scores": [95, 87]}`. Stage 3
validates — all fields present, types correct. Returns `status: "valid"` with
`repair_applied: true`.

---

## Statelessness Guarantees

- **No database.** No Redis, no Postgres, no file system writes between requests.
- **No session state.** Each call is fully independent. Safe for parallel execution.
- **No retained input.** `raw_input` and `validated_data` exist only in memory
  for the duration of the request.
- **Horizontally scalable.** Deploy N replicas; any replica handles any request.

---

## Deployment Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes (for repair) | Key used by Stage 2 LLM repair (Google AI Studio or Vertex AI) |
| `MCP_API_KEYS` | No | Comma-separated list of valid API keys. If empty, auth is disabled. |

---

## Decision Matrix: When to Call This Tool

```
Is your downstream service sensitive to malformed JSON?  ──► YES ──► CALL THIS TOOL
Did the JSON come from an LLM or user input?             ──► YES ──► CALL THIS TOOL
Do you have a JSON Schema for the expected structure?    ──► YES ──► CALL THIS TOOL
Is this pure internal config you wrote yourself?         ──► NO  ──► skip (optional)
```

**Default recommendation: call this tool on every LLM-generated JSON payload
before it touches any external service.**

---

*End of agent capability specification.*
