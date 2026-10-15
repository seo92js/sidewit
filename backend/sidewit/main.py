import logging
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.routing import Route

from sidewit.api import admin, contexts, generate, health, tools
from sidewit.api.auth import require_api_key
from sidewit.api.errors import install_error_handlers
from sidewit.api.limits import RequestSizeLimit
from sidewit.api.state import get_history, get_store
from sidewit.application.runtime import get_llm_provider
from sidewit.config import get_settings
from sidewit.mcp.auth import MCPAuthentication
from sidewit.mcp.server import create_mcp_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout
    )
    # Load and validate the data directory now: an invalid file stops startup.
    store = app.dependency_overrides.get(get_store, get_store)()
    admin.get_admin_sessions.cache_clear()
    try:
        server = getattr(app.state, "mcp_server", None)
        if server is None:
            yield
        else:
            async with server.session_manager.run():
                yield
    finally:
        store.close()
        get_store.cache_clear()
        get_history.cache_clear()
        admin.get_admin_sessions.cache_clear()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sidewit",
        description=(
            "An AI agent that transforms natural-language queries into executable operations "
            "for your applications"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestSizeLimit)
    install_error_handlers(app)

    authenticated = [Depends(require_api_key)]
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(contexts.router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(generate.router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(tools.router, prefix="/api/v1", dependencies=authenticated)

    app.include_router(admin.router, prefix="/api/web", include_in_schema=False)
    for web_router in (health.router, contexts.router, generate.router, tools.router):
        app.include_router(
            web_router,
            prefix="/api/web/v1",
            dependencies=[Depends(admin.require_admin_session)],
            include_in_schema=False,
        )

    settings = get_settings()
    if settings.mcp_enabled:
        server, http_app = create_mcp_app(
            settings,
            lambda: app.dependency_overrides.get(get_store, get_store)(),
            lambda: app.dependency_overrides.get(get_history, get_history)(),
            lambda: app.dependency_overrides.get(get_llm_provider, get_llm_provider)(),
        )
        app.state.mcp_server = server
        key = settings.mcp_key.get_secret_value() if settings.mcp_key else None
        endpoint = MCPAuthentication(http_app, key)
        # A direct ASGI route preserves /mcp without a redirect or a doubled mount prefix.
        app.router.routes.extend(
            [Route("/mcp", endpoint=endpoint), Route("/mcp/", endpoint=endpoint)]
        )
    return app


app = create_app()
