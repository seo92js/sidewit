from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sidewit.application.storage import storage_failure
from sidewit.errors import SidewitError
from sidewit.observability import annotate
from sidewit.store import StoreError


def _body(error: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": error, "message": message, "details": details}


async def _api_error(_: Request, exc: SidewitError) -> JSONResponse:
    annotate(error_code=exc.error)
    return JSONResponse(_body(exc.error, exc.message, exc.details), status_code=exc.status_code)


async def _request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    annotate(error_code="invalid_request")
    errors = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(
        _body("invalid_request", "Request validation failed.", {"errors": errors}),
        status_code=422,
    )


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    error = "not_found" if exc.status_code == 404 else "http_error"
    annotate(error_code=error)
    return JSONResponse(_body(error, str(exc.detail), {}), status_code=exc.status_code)


async def _store_error(request: Request, exc: StoreError) -> JSONResponse:
    from sidewit.api.state import get_store

    factory = request.app.dependency_overrides.get(get_store, get_store)
    return await _api_error(request, storage_failure(factory, exc))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SidewitError, _api_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(StoreError, _store_error)
