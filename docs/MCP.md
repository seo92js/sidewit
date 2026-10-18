# MCP integration

Sidewit optionally serves MCP over **Streamable HTTP at `/mcp`**, in the same process and
container as the REST API. There is no extra MCP container. Both transports share generation,
validation, saved Tools, the data-directory lock and temporary Generation history.

MCP returns **SQL operations and parameters, never database results**. Your application must
review and execute them with appropriate database permissions and limits. Connecting Sidewit
to ChatGPT or Claude does not give Sidewit a database connection or an execution capability.

## Enable and connect

Set these values in the existing root `.env` file:

```dotenv
SIDEWIT_MCP_ENABLED=true
SIDEWIT_MCP_KEY=<your-separate-mcp-key>
```

Rebuild for the first installation, or recreate the API container after configuration changes:

```bash
docker compose up -d --build --wait
```

Use **`http://127.0.0.1:8000/mcp`** locally. The optional web container also proxies MCP at
**`http://127.0.0.1:3000/mcp`**. Configure a Streamable HTTP client with this header:

```text
Authorization: Bearer <your-separate-mcp-key>
```

MCP is disabled by default (`/mcp` returns 404). When explicitly enabled with an empty
`SIDEWIT_MCP_KEY`, it permits unauthenticated access. This is independent of the REST API key
and browser login: neither authenticates MCP, and an MCP key does not authenticate REST or
the web UI. MCP access covers every Context and eligible Tool in this instance; it is not
per-user or per-Context authorization. Keep unauthenticated deployments private.

## Remote connectors

A cloud connector needs an HTTPS endpoint reachable from that service, for example
`https://sidewit.example.com/mcp`. Its `localhost` is not your computer. Put a private ingress
or an authenticated deployment in front of any sensitive Contexts. Keep the supplied Docker
port bindings on loopback and route your ingress to them.

Add the exact incoming Host header to the JSON array in `.env`; include a port if the public
URL uses a non-default port. Preserve Host at every reverse proxy:

```dotenv
SIDEWIT_MCP_ALLOWED_HOSTS=["localhost","localhost:*","127.0.0.1","127.0.0.1:*","sidewit.example.com"]
```

Unexpected Hosts return 421. Requests without an Origin header are accepted if Host is valid.
If a trusted client sends Origin, explicitly allow that exact origin:

```dotenv
SIDEWIT_MCP_ALLOWED_ORIGINS=["https://trusted-client.example.com"]
```

Unexpected Origins return 403. This allowlist does not enable cross-origin browser CORS.
The bundled nginx configuration preserves Host, disables response buffering and uses a
100-second read timeout. Configure an external ingress with the same behavior and a timeout
longer than the 90-second generation deadline. Every request is limited to 1 MiB.

Client authentication capabilities differ:

- Claude custom remote connectors provide request headers for fixed API keys/Bearer tokens;
  configure the Authorization header above. See [Claude's connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
- ChatGPT custom MCP connections support OAuth and no authentication. Do not assume their
  connector UI accepts an arbitrary Bearer key. Sidewit currently provides static Bearer or
  no authentication, **not OAuth**. Use no-auth only where exposing this instance is acceptable,
  or supply an appropriate OAuth gateway. See [OpenAI's MCP guide](https://developers.openai.com/api/docs/guides/custom-mcp-server).

These are configuration instructions, not a claim that this release has been acceptance-tested
in ChatGPT or Claude. The automated integration suite uses the official MCP SDK client and
also checks the 2025-03-26, 2025-06-18 and 2025-11-25 Streamable HTTP handshakes.

## Four MCP tools

| MCP tool | Arguments | Result |
|---|---|---|
| `list_contexts` | Optional `limit`, `offset` | Context IDs, names, versions, dialect, timezone, generation policy and resource counts |
| `list_tools` | `context_id`; optional `limit`, `offset` | Enabled, currently valid saved Tools with IDs, versions, descriptions and input schemas |
| `generate_operations` | The same fields as `POST /api/v1/generate` | `ok`, `ambiguous` or `unanswerable`, with the same payload as REST |
| `invoke_tool` | `tool_id`, `arguments`; optional `expected_version` | The same operation and validation as REST Tool invocation |

Discovery returns `{items, next_offset}`. Default page size is 50, maximum 100; pass
`next_offset` as the next request's `offset` until it is null. Pages reflect current state,
not a snapshot. Tool names are editable labels; use immutable Tool UUIDs and pass the discovered
`version` as `expected_version` to detect edits before invocation. Disabled, invalid and stale
Tools are excluded from discovery; invocation always checks current availability and validity.
Discovery does not change saved validation status. If a previously accepted Tool fails current
validation, invocation rejects it and saves its `invalid` status, including after MCP discovery.
There are no MCP tools for administrative create/update/delete, debug history or SQL execution.

A typical interaction is:

1. `list_contexts` to choose a Context.
2. `generate_operations` with `{"context_id":"<uuid>","question":"서울 매장 개수"}`.
3. Review every `operations` entry. Steps are independent; Sidewit does not combine results.

Alternatively, discover saved Tools and use `invoke_tool` directly. Invocation does not call
an LLM. Generation requires a configured provider and can mix saved Tools with newly generated
operations. See [API usage](API_USAGE.md) for request options and validation boundaries.

## Clarification and errors

For `ambiguous`, ask the returned `clarification_questions`. Take `continuation_request`,
add or replace `{id, question, answer}` entries in its `clarification_answers`, and pass that
complete object to `generate_operations`. Preserve its original question, reference time,
Context version and options. There is no MCP conversation session to resume: the request
contains the continuation state. See [clarification limits](CLARIFICATIONS.md).

Tool responses contain the same JSON in `structuredContent` and a text content block.
Their `_meta` includes `sidewit/request_id` for the tool call and
`sidewit/parent_request_id` for its HTTP envelope (`X-Request-ID`). Save these IDs to correlate
application records with [operational logs](OBSERVABILITY.md); they do not appear in the
content payload. MCP tool status and HTTP transport status are recorded separately.
Application failures set MCP `isError: true` and return:

```json
{
  "error": "version_conflict",
  "message": "Tool was modified by someone else.",
  "details": {"current_version": 2, "expected_version": 1},
  "status_code": 409
}
```

`status_code` is application error metadata; it is not the HTTP status of a successful MCP
protocol exchange. Missing/incorrect MCP authentication instead returns HTTP 401 before
protocol processing. `ambiguous` and `unanswerable` are normal tool results, not errors.
On `context_changed`, start a new request with the current Context rather than automatically
replaying old answers. Rejected SQL and raw internal/provider errors are not exposed.
