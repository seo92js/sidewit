"""Bound API input before parsing; the model validators impose smaller domain limits."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_BODY_BYTES = 1024 * 1024


class RequestSizeLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                response = JSONResponse(
                    {
                        "error": "request_too_large",
                        "message": "Request body exceeds 1 MiB.",
                        "details": {},
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
