import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sidewit.store import StoreError

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self, status_code: int, error: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.details = details or {}


def _body(error: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": error, "message": message, "details": details}


async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(_body(exc.error, exc.message, exc.details), status_code=exc.status_code)


async def _request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(
        _body("invalid_request", "Request validation failed.", {"errors": errors}),
        status_code=422,
    )


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    error = "not_found" if exc.status_code == 404 else "http_error"
    return JSONResponse(_body(error, str(exc.detail), {}), status_code=exc.status_code)


async def _store_error(request: Request, exc: StoreError) -> JSONResponse:
    from sidewit.api.state import get_store

    recovery_error = None
    try:
        store = request.app.dependency_overrides.get(get_store, get_store)()
        store.restore()
    except StoreError as error:
        recovery_error = error
    _log_storage_error(exc, "succeeded" if recovery_error is None else "failed")
    if recovery_error is not None:
        _log_storage_error(recovery_error, "unavailable")
    return JSONResponse(
        _body(
            "storage_unavailable", "Storage failed. Re-read current versions before retrying.", {}
        ),
        status_code=503,
    )


def _log_storage_error(error: StoreError, recovery: str) -> None:
    # Exception messages and paths can contain user data; log only diagnostic metadata.
    cause = error.__cause__ or error
    log.error(
        "storage_failure operation=%s error_type=%s errno=%s recovery=%s",
        error.operation,
        type(cause).__name__,
        cause.errno if isinstance(cause, OSError) else None,
        recovery,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(StoreError, _store_error)
