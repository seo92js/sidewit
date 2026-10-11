import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"


class _SqlDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SQLGlot warnings and optimizer errors can contain arbitrary SQL/data, not only
        # credential-shaped strings. Keep a diagnostic event, never its original payload.
        record.msg = "SQLGlot diagnostic omitted because it may contain SQL or private values."
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def protect_sql_logs() -> None:
    logger = logging.getLogger("sqlglot")
    if not any(isinstance(f, _SqlDiagnosticFilter) for f in logger.filters):
        logger.addFilter(_SqlDiagnosticFilter())


# Credential shapes that provider SDK/server error messages may echo back.
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_*\-]{4,}"), REDACTED),  # OpenAI keys, incl. partially masked
    (re.compile(r"AIza[0-9A-Za-z_\-]{10,}"), REDACTED),  # Google API keys
    (re.compile(r"(?i)\b(bearer\s+)[^\s'\",}]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b((?:api[_-]?)?key=)[^&\s'\"]+"), r"\1" + REDACTED),
]


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Removes known secret values and credential-looking tokens from `text`."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
