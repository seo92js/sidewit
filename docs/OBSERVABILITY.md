# Operational logs and application audit records

Sidewit writes content-free operational events as one JSON object per line to standard output.
It does not need a logging database and does not write audit records into your application's
database. Use the deployment's existing log collector for search, retention and access control.

## Find a request

Every HTTP request reaching the API receives a new, server-generated `X-Request-ID` response
header, including authentication failures, invalid input, oversized bodies and generation
timeouts. Incoming `X-Request-ID` values are not adopted. Response bodies are unchanged.
Requests rejected by a reverse proxy before reaching Sidewit do not receive this header.

```bash
curl -i http://127.0.0.1:8000/api/v1/health
docker compose logs --no-log-prefix api
```

Read the response header and find the same `request_id` in `request_completed` events:

```json
{
  "event": "request_completed",
  "request_id": "9a3c890f-cd97-4133-bae3-ed8a708cc4f4",
  "transport": "rest",
  "action": "generate",
  "status_code": 200,
  "status": "ok",
  "context_id": "9f49c2c4-bb20-490c-8a43-725e704f297b",
  "context_version": 2,
  "generation_id": "004c0661-7bcd-435e-8a29-e052166c21cc",
  "llm_calls": 2,
  "token_usage_complete": false,
  "duration_ms": 1250
}
```

Events also include a UTC `timestamp`, `level` and `logger`. Fields are included when known:

- `transport`: `rest`, `web` or `mcp`; `action` identifies generation, invocation or discovery.
- `route`: a route template (possibly relative to its router), never the raw URL or query string.
- `error_code`: the application error code, or `http_<status>` for transport failures without
  an application code. Unexpected errors include only `error_type`, not exception text.
- For HTTP events, `status_code` preserves the status once headers have been sent successfully.
  `response_complete` says whether the final response body was sent. A later failure records
  `status: error` and the original exception type at ERROR level, even if the sent status was
  200. It does not rewrite an already-sent status or send a second response. Errors before
  headers can still produce the normal generic JSON 500 response.
- `generation_id`, Context/Tool IDs and versions, operation count and selected Tool ID/version
  pairs. Generation IDs are attached before provider calls, so timeouts can be correlated even
  if no Generation survives in the temporary history. Pre-generation failures have no such ID.
- Provider/model, `llm_calls`, and reported input/output token totals. Calls include selection,
  generation, repair, failed and cancelled provider invocations. Deterministic invocation has
  zero LLM calls. `token_usage_complete: false` means at least one call did not report both
  counts; totals then cover only reported usage, not a complete billable usage estimate.

One HTTP completion event is emitted per request; the history cache does not emit a duplicate
Generation log. A disconnected/cancelled request may have error code `client_disconnected`
or `request_cancelled` and status 499 before headers were sent, which is diagnostic metadata,
not a response sent to the disconnected client. Cancellation after headers preserves the
sent HTTP status and marks `status: cancelled`.
Process termination can prevent a final log from being emitted; these logs are not durable
audit evidence.

## MCP correlation

Each MCP tool call has its own completion event, `mcp_tool_completed`, and result metadata:

```json
{
  "_meta": {
    "sidewit/request_id": "<tool-call-id>",
    "sidewit/parent_request_id": "<http-request-id>"
  }
}
```

`sidewit/request_id` identifies the logical tool call. `sidewit/parent_request_id` links it to
the HTTP envelope and its `X-Request-ID`. This also works for tool failures. Text content and
`structuredContent` retain their existing payloads. HTTP status 200 may carry an MCP tool
failure: inspect the tool event's `error_code`/`status_code` and the result's `isError`.
Protocol handshakes and authentication/Host/Origin failures have HTTP observations, even when
no tool callback ran. The HTTP completion event describes transport success, not tool success.
For MCP, provider usage and operation metadata belong to the tool-call event; the HTTP
envelope does not duplicate those counters.

## Privacy and retention

Logs omit questions, clarification answers, SQL, parameter values, Context/Tool descriptions,
request/response bodies, cookies, credentials and raw exception messages/tracebacks.
Third-party diagnostics retain source, severity and exception type when available, with their
text omitted. The API's Uvicorn access logger and bundled nginx access log are disabled because
raw URLs/query strings can contain sensitive values. These content guarantees apply to the
Python application's logs. nginx error diagnostics and external ingress/collectors have their
own logging policies; do not send secrets in URLs and configure their logging accordingly.
Server startup messages before logging initialization may use the server's native format.

At application startup, Sidewit applies JSON formatting and a metadata-only record filter to
registered Python logging handlers, including existing file handlers, stream subclasses and
handlers on named loggers. This also protects exporters that bypass formatters: original
messages, arguments, arbitrary extra fields and exception objects are removed from the record
delivered to each handler. Existing destinations are kept, and stdout is available without
adding duplicate Sidewit console handlers on repeated initialization. Install custom handlers
before the ASGI lifespan starts; if adding them later, call
`sidewit.observability.configure_logging()` again to apply the same policy.

Docker Compose rotates logs for each API/web container (`max-size: 10m`, `max-file: 3`).
Recreate containers to apply changed logging options. These are local troubleshooting logs,
not permanent storage. Choose centralized retention when needed. Metadata such as Context IDs,
Tool IDs and model names can still reveal deployment details; restrict log access.

## Store application audit records yourself

Your application knows the actual user, tenant, approval decision and database execution result;
Sidewit knows only operation preparation. If needed, store the original request, selected
response fields or an operation snapshot, `generation_id`, `request_id`, and relevant versions
in your own audit table. After execution, attach execution success/failure and timing there.
For MCP use the tool-call ID and, optionally, its parent HTTP ID.

For example, using your application's own audit component (pseudocode):

```python
response = http.post("/api/v1/generate", json=request)
audit_id = your_audit_store.create(
    user_id=user.id,
    request_id=response.headers.get("X-Request-ID"),
    generation_id=response.json().get("generation_id"),
    preparation_status=response.status_code,
)
# Later, after your application approves and executes the selected operation:
your_audit_store.complete(audit_id, execution_status="succeeded")
```

Choose whether to retain SQL, bound values and results according to your application's policy;
there is no requirement to store complete response/result bodies. Sidewit returns no audit
INSERT operation and receives no execution-result callback. Its temporary Generation history
remains bounded and disappears on restart.
