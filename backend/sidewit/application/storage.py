"""Recover shared storage after a failed operation, without exposing file contents."""

import logging
from collections.abc import Callable

from sidewit.errors import SidewitError
from sidewit.store import Store, StoreError

log = logging.getLogger(__name__)


def storage_failure(get_store: Callable[[], Store], error: StoreError) -> SidewitError:
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
    log.error(
        "storage_failure operation=%s error_type=%s errno=%s recovery=%s",
        error.operation,
        type(cause).__name__,
        cause.errno if isinstance(cause, OSError) else None,
        recovery,
    )
