"""Observe HTTP envelopes without reading or logging request/response bodies."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sidewit.observability import annotate, observe, record_failure


class RequestObservation:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        transport = "web" if path.startswith("/api/web/") else "rest"
        if path in {"/mcp", "/mcp/"}:
            transport = "mcp"
        with observe("request_completed", transport=transport, action="http") as observation:
            annotate(response_complete=False)
            method = scope["method"]
            annotate(
                method=method
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )

            async def respond(message: Message) -> None:
                if message["type"] == "http.response.start":
                    code = message["status"]
                    headers = [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"x-request-id"
                    ]
                    headers.append((b"x-request-id", observation.request_id.encode("ascii")))
                    message = {**message, "headers": headers}
                await send(message)
                if message["type"] == "http.response.start":
                    observation.http_status_code = code
                    annotate(status_code=code)
                    if code >= 400:
                        annotate(status="error")
                        if "error_code" not in observation.fields:
                            annotate(error_code=f"http_{code}")
                elif message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    annotate(response_complete=True)

            try:
                await self.app(scope, receive, respond)
                if observation.http_status_code is None:
                    annotate(status="cancelled", error_code="client_disconnected", status_code=499)
            except Exception as exc:
                # Do not forward exception text to Uvicorn's traceback logger.
                record_failure(exc)
                if observation.http_status_code is None:
                    response = JSONResponse(
                        {"error": "internal_error", "message": "Request failed.", "details": {}},
                        status_code=500,
                    )
                    await response(scope, receive, respond)
                elif not observation.fields["response_complete"]:
                    raise RuntimeError("Response interrupted.") from None
            finally:
                # Route templates contain no user-controlled IDs, names or query strings.
                route = scope.get("route")
                annotate(route=getattr(route, "path", "unmatched"))
