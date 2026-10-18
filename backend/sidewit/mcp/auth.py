"""Bearer authentication for every HTTP method on the MCP endpoint."""

import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sidewit.observability import annotate


class MCPAuthentication:
    def __init__(self, app: ASGIApp, key: str | None) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.key:
            authorization = Headers(scope=scope).get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                token.encode(), self.key.encode()
            ):
                annotate(error_code="unauthorized")
                response = JSONResponse(
                    {
                        "error": "unauthorized",
                        "message": "Missing or invalid MCP key.",
                        "details": {},
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
