"""Recover shared storage after a failed operation, without exposing file contents."""

import logging
from collections.abc import Callable

from sidewit.errors import SidewitError
from sidewit.observability import emit
from sidewit.store import Store, StoreError, StoreUnavailable


def retry_recovery(store: Store) -> None:
    """Reload after a failed recovery, at most once per RESTORE_INTERVAL_SECONDS."""
    if store.restore_due():
        try:
            store.restore()
        except StoreError as error:
            _log(error, "unavailable")


def storage_failure(get_store: Callable[[], Store], error: StoreError) -> SidewitError:
    """503 for any storage error; only a failed change reloads the files."""
    if isinstance(error, StoreUnavailable):
        return SidewitError(
            503,
            "storage_unavailable",
            "Storage is unavailable. Recovery is retried automatically; repair the data "
            "directory if this persists.",
        )
    try:
        get_store().restore()
    except StoreError as recovery_error:
        _log(error, "failed")
        _log(recovery_error, "unavailable")
    else:
        _log(error, "succeeded")
    return SidewitError(
        503, "storage_unavailable", "Storage failed. Re-read current versions before retrying."
    )


def _log(error: StoreError, recovery: str) -> None:
    cause = error.__cause__ or error
    emit(
        "storage_failure",
        level=logging.ERROR,
        operation=error.operation,
        error_type=type(cause).__name__,
        errno=cause.errno if isinstance(cause, OSError) else None,
        recovery=recovery,
    )
