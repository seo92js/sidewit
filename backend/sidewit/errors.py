"""Transport-independent errors with stable public codes."""

from typing import Any


class SidewitError(Exception):
    def __init__(
        self, status_code: int, error: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.details = details or {}
