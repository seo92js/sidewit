"""Content-free operational events and request-local correlation, not an audit store."""

import asyncio
import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sidewit.errors import SidewitError

log = logging.getLogger(__name__)
EVENTS = frozenset({"request_completed", "mcp_tool_completed", "store_loaded", "storage_failure"})

# Only explicitly selected metadata crosses the logging boundary. Never accept a
# request/response object, arbitrary headers, an exception message or a SQL statement.
FIELDS = frozenset(
    "request_id parent_request_id transport action route method status_code status error_code "
    "error_type duration_ms generation_id context_id context_version tool_id tool_version "
    "provider model llm_calls input_tokens output_tokens token_usage_complete "
    "operation_count selected_tools operation recovery errno contexts tools "
    "response_complete".split()
)
type LogValue = str | int | bool | None | list[dict[str, str | int]]


def _event_data(record: logging.LogRecord) -> dict[str, LogValue]:
    event = getattr(record, "event_name", None)
    trusted = record.name == __name__ and event in EVENTS
    value: dict[str, LogValue] = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "event": event if trusted else "diagnostic",
    }
    if trusted:
        value.update({k: v for k, v in getattr(record, "event_fields", {}).items() if k in FIELDS})
    elif observation := current():
        value["request_id"] = observation.request_id
    # Never retain third-party messages, arguments or traceback contents.
    if record.exc_info:
        value["error_type"] = record.exc_info[0].__name__
    return value


class _SafeRecord(logging.LogRecord):
    def __init__(self, original: logging.LogRecord) -> None:
        value = _event_data(original)
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False)
        super().__init__(original.name, original.levelno, "", 0, payload, (), None)
        self.created = original.created
        self.event_name = value["event"]
        self.event_fields = {k: v for k, v in value.items() if k in FIELDS}


class _SafeRecordFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # Python 3.12 handler filters can replace a record. This also protects queue,
        # socket and custom handlers that export records without calling a formatter.
        return record if isinstance(record, _SafeRecord) else _SafeRecord(record)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record, _SafeRecord):
            return record.getMessage()
        return json.dumps(_event_data(record), ensure_ascii=True, allow_nan=False)


class _ConsoleHandler(logging.StreamHandler):
    """Identify our stdout handler without replacing an operator's file/queue handlers."""


def configure_logging() -> None:
    formatter = JsonLogFormatter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # The application owns this namespace even when a host previously muted it.
    log.disabled = False
    log.setLevel(logging.INFO)
    log.propagate = True
    logging.getLogger("sidewit").propagate = True
    # Rebind only our console if a host changes stdout between application lifetimes.
    for handler in root.handlers[:]:
        if isinstance(handler, _ConsoleHandler) and handler.stream is not sys.stdout:
            root.removeHandler(handler)
            handler.close()
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stdout for h in root.handlers
    ):
        root.addHandler(_ConsoleHandler(sys.stdout))
    # basicConfig is a no-op when logging was already configured. Named loggers can
    # also own handlers instead of propagating to root. Format every registered sink,
    # including file handlers and subclasses, and do not multiply handlers on restart.
    loggers = [
        root,
        *(
            logger
            for logger in list(root.manager.loggerDict.values())
            if isinstance(logger, logging.Logger)
        ),
    ]
    for logger in loggers:
        for handler in logger.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(f, _SafeRecordFilter) for f in handler.filters):
                handler.addFilter(_SafeRecordFilter())
    # Access logs include uncontrolled URL paths/query strings and duplicate our summary.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("httpx", "httpx2", "httpcore", "mcp", "openai", "google.genai"):
        logging.getLogger(name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class _Failure:
    status: str
    code: str
    status_code: int
    error_type: str


@dataclass
class Observation:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    fields: dict[str, LogValue] = field(default_factory=dict)
    usage_reports: int = 0
    http_status_code: int | None = None
    failure: _Failure | None = None


_current: ContextVar[Observation | None] = ContextVar("sidewit_observation", default=None)


def current() -> Observation | None:
    return _current.get()


def annotate(**fields: LogValue) -> None:
    observation = current()
    if observation is not None:
        observation.fields.update({k: v for k, v in fields.items() if k in FIELDS})


def record_failure(exc: BaseException) -> None:
    observation = current()
    if observation is None:
        return
    if observation.failure is None:
        if isinstance(exc, asyncio.CancelledError):
            status, code, status_code = "cancelled", "request_cancelled", 499
        elif isinstance(exc, SidewitError):
            status, code, status_code = "error", exc.error, exc.status_code
        else:
            status, code, status_code = "error", "internal_error", 500
        # Retain only diagnostic metadata, never the exception or its private traceback.
        observation.failure = _Failure(status, code, status_code, type(exc).__name__)
    failure = observation.failure
    annotate(
        status=failure.status,
        error_code=failure.code,
        error_type=failure.error_type,
        status_code=(
            observation.http_status_code
            if observation.http_status_code is not None
            else failure.status_code
        ),
    )


def emit(event: str, *, level: int = logging.INFO, **fields: LogValue) -> None:
    observation = current()
    metadata: dict[str, LogValue] = {}
    if observation:
        if event in {"request_completed", "mcp_tool_completed"}:
            metadata.update(observation.fields)
        else:
            # Intermediate storage diagnostics must not inherit the request's unfinished
            # status/duration/counters and look like another completion event.
            for key in ("transport", "parent_request_id"):
                if key in observation.fields:
                    metadata[key] = observation.fields[key]
        metadata["request_id"] = observation.request_id
    metadata.update({k: v for k, v in fields.items() if k in FIELDS})
    log.log(level, event, extra={"event_name": event, "event_fields": metadata})


@contextmanager
def observe(event: str, *, transport: str, action: str) -> Iterator[Observation]:
    parent = current()
    observation = Observation(
        fields={"transport": transport, "action": action, "status": "ok", "llm_calls": 0}
    )
    if parent:
        observation.fields["parent_request_id"] = parent.request_id
    token = _current.set(observation)
    started = time.monotonic()
    try:
        yield observation
    except (Exception, asyncio.CancelledError) as exc:
        record_failure(exc)
        raise
    finally:
        if "status_code" not in observation.fields:
            annotate(status_code=200)
        annotate(duration_ms=round((time.monotonic() - started) * 1000))
        try:
            status_code = observation.fields.get("status_code", 200)
            level = (
                logging.ERROR
                if status_code >= 500
                or (observation.failure is not None and observation.failure.status_code >= 500)
                else logging.INFO
            )
            emit(event, level=level)
        finally:
            _current.reset(token)
