"""File storage for Contexts and Tools (SPEC §11). No database.

Records live in memory and are written through to one JSON file each:

    <data_dir>/contexts/<context_id>.json
    <data_dir>/tools/<tool_name>.json

Concurrency model: one process owns the directory. Every mutation runs synchronously on the
event loop (no `await` between reading the current state and writing the file), so a request
never observes or overwrites another request's half-done change. This replaces database row
locks. Each file is replaced atomically (write a temporary file, fsync, rename).
"""

import fcntl
import json
import logging
import os
import tempfile
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from sidewit.domain.context import ContextDefinition, definition_issues, generation_issues
from sidewit.domain.dialect import Dialect
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding
from sidewit.domain.tool import TOOL_NAME_PATTERN, ToolOperation, ToolStatus
from sidewit.validation.input_schema import input_schema_issues
from sidewit.validation.sql_validator import ValidationResult

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ContextRecord:
    name: str
    dialect: Dialect
    definition: dict[str, Any]
    timezone: str = "UTC"
    allow_generated_sql: bool = True
    version: int = 1
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class ToolRecord:
    context_id: uuid.UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    operation: dict[str, Any]
    context_version: int
    status: str
    version: int = 1
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    source_generation_id: uuid.UUID | None = None
    metric_binding: dict[str, Any] | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


class StoreError(Exception):
    """The data directory could not be read or written."""

    def __init__(
        self,
        message: str,
        *,
        operation: Literal["open", "read", "write", "delete", "validate", "access"] = "access",
    ) -> None:
        super().__init__(message)
        self.operation = operation


class _ContextFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str = Field(min_length=1)
    dialect: Dialect
    timezone: str
    allow_generated_sql: bool
    definition: ContextDefinition
    version: int = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as e:
            raise ValueError("Unknown IANA timezone") from e
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("Context name must be nonblank and trimmed")
        return value


class _ToolFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    context_id: uuid.UUID
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    operation: ToolOperation
    version: int = Field(ge=1)
    context_version: int = Field(ge=1)
    status: ToolStatus
    validation_errors: list[Issue]
    validation: ValidationResult | None
    source_generation_id: uuid.UUID | None
    metric_binding: MetricBinding | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("input_schema")
    @classmethod
    def _inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if input_schema_issues(value):
            raise ValueError("Invalid Tool input_schema")
        return value


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._contexts_dir = root / "contexts"
        self._tools_dir = root / "tools"
        self.contexts: dict[uuid.UUID, ContextRecord] = {}
        self.tools: dict[str, ToolRecord] = {}
        # Structural verdicts belong to this snapshot; actual invoke values are always checked.
        self._tool_checks: OrderedDict[tuple[uuid.UUID, int, uuid.UUID, int], ValidationResult] = (
            OrderedDict()
        )
        self._available = False
        self._lock = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            self._lock = (root / ".lock").open("a")
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._load()
        except (OSError, StoreError) as e:
            self.close()
            if isinstance(e, StoreError):
                raise
            raise StoreError(
                "Data directory is unavailable or already owned by another process",
                operation="open",
            ) from e

    def close(self) -> None:
        self._available = False
        self._tool_checks.clear()
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def require_available(self) -> None:
        if not self._available:
            raise StoreError("Storage recovery failed; restart after repairing the data directory")

    # reads ----------------------------------------------------------------------------

    def context(self, context_id: uuid.UUID) -> ContextRecord | None:
        return deepcopy(self.contexts.get(context_id))

    def context_named(self, name: str) -> ContextRecord | None:
        return deepcopy(next((c for c in self.contexts.values() if c.name == name), None))

    def tool(self, name: str) -> ToolRecord | None:
        return deepcopy(self.tools.get(name))

    def tools_of(self, context_id: uuid.UUID) -> list[ToolRecord]:
        return sorted(
            (deepcopy(t) for t in self.tools.values() if t.context_id == context_id),
            key=lambda t: t.name,
        )

    def healthy(self) -> bool:
        """The directory is readable and writable."""
        if not self._available:
            return False
        try:
            for directory in (self.root, self._contexts_dir, self._tools_dir):
                _json_files(directory)
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".health-"):
                    pass
            return self._contexts_dir.is_dir() and self._tools_dir.is_dir()
        except (OSError, StoreError):
            return False

    def tool_check(self, tool: ToolRecord, context: ContextRecord) -> ValidationResult | None:
        key = (tool.id, tool.version, context.id, context.version)
        result = self._tool_checks.get(key)
        if result is not None:
            self._tool_checks.move_to_end(key)
        return deepcopy(result)

    def remember_tool_check(
        self, tool: ToolRecord, context: ContextRecord, result: ValidationResult
    ) -> None:
        key = (tool.id, tool.version, context.id, context.version)
        self._tool_checks[key] = deepcopy(result)
        self._tool_checks.move_to_end(key)
        while len(self._tool_checks) > 1024:
            self._tool_checks.popitem(last=False)

    # writes ---------------------------------------------------------------------------

    def save_context(self, record: ContextRecord) -> None:
        self.require_available()
        for key in list(self._tool_checks):
            if key[2] == record.id:
                del self._tool_checks[key]
        record.updated_at = _now()
        self._write(self._contexts_dir / f"{record.id}.json", record)
        self.contexts[record.id] = deepcopy(record)

    def save_tool(self, record: ToolRecord) -> None:
        self.require_available()
        self._forget_tool_checks(record.id)
        record.updated_at = _now()
        self._write(self._tools_dir / f"{record.name}.json", record)
        self.tools[record.name] = deepcopy(record)

    def delete_context(self, record: ContextRecord) -> None:
        self.require_available()
        self._unlink(self._contexts_dir / f"{record.id}.json")
        self.contexts.pop(record.id, None)

    def delete_tool(self, record: ToolRecord) -> None:
        self.require_available()
        self._forget_tool_checks(record.id)
        self._unlink(self._tools_dir / f"{record.name}.json")
        self.tools.pop(record.name, None)

    def _forget_tool_checks(self, tool_id: uuid.UUID) -> None:
        for key in list(self._tool_checks):
            if key[0] == tool_id:
                del self._tool_checks[key]

    def restore(self) -> None:
        """Reload memory from disk after a failed write left a record changed in memory."""
        self._available = False
        self._tool_checks.clear()
        self._load()

    # files ----------------------------------------------------------------------------

    def _write(self, path: Path, record: ContextRecord | ToolRecord) -> None:
        data = json.dumps(
            _to_json(record), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        try:
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
                _sync_directory(path.parent)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as e:
            raise StoreError(f"Could not write {path.name}: {e.strerror}", operation="write") from e

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            _sync_directory(path.parent)
        except OSError as e:
            raise StoreError(
                f"Could not delete {path.name}: {e.strerror}", operation="delete"
            ) from e

    def _load(self) -> None:
        """Read and validate every file. Any problem stops startup with the file named."""
        try:
            self._contexts_dir.mkdir(parents=True, exist_ok=True)
            self._tools_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(
                f"Data directory {self.root} is not usable: {e.strerror}", operation="open"
            ) from e
        problems: list[str] = []
        contexts: dict[uuid.UUID, ContextRecord] = {}
        tools: dict[str, ToolRecord] = {}
        for path in _json_files(self._contexts_dir):
            parsed = _parse(path, _ContextFile, problems)
            if parsed is None:
                continue
            issues = definition_issues(parsed.definition) + generation_issues(
                parsed.definition, parsed.allow_generated_sql
            )
            if issues:
                problems += [f"{path}: {i.ref}: {i.message}" for i in issues]
            elif path.stem != str(parsed.id):
                problems.append(f"{path}: file name must be the Context id {parsed.id}")
            elif any(c.name == parsed.name for c in contexts.values()):
                problems.append(f"{path}: another Context is named '{parsed.name}'")
            else:
                record = ContextRecord(**_fields(parsed, ContextRecord))
                record.definition = parsed.definition.to_json()
                contexts[record.id] = record
        for path in _json_files(self._tools_dir):
            parsed = _parse(path, _ToolFile, problems)
            if parsed is None:
                continue
            if path.stem != parsed.name:
                problems.append(f"{path}: file name must be the Tool name '{parsed.name}'")
            elif parsed.context_id not in contexts:
                problems.append(f"{path}: Context {parsed.context_id} does not exist")
            elif any(t.id == parsed.id for t in tools.values()):
                problems.append(f"{path}: duplicate Tool id {parsed.id}")
            else:
                tools[parsed.name] = ToolRecord(**_fields(parsed, ToolRecord))
        if problems:
            raise StoreError(
                "Invalid data files:\n" + "\n".join(f"  {p}" for p in problems),
                operation="validate",
            )
        self.contexts, self.tools = contexts, tools
        self._available = True
        log.info("Loaded %d Contexts and %d Tools", len(self.contexts), len(self.tools))


def _json_files(directory: Path) -> list[Path]:
    # glob suppresses directory read errors, which would turn unreadable data into an empty store.
    try:
        with os.scandir(directory) as entries:
            return sorted(Path(entry.path) for entry in entries if entry.name.endswith(".json"))
    except OSError as e:
        raise StoreError(
            f"Could not read directory {directory}: {e.strerror}", operation="read"
        ) from e


def _parse[M: BaseModel](path: Path, model: type[M], problems: list[str]) -> M | None:
    try:
        return model.model_validate_json(path.read_bytes())
    except OSError as e:
        problems.append(f"{path}: {e.strerror}")
    except ValidationError as e:
        problems += [
            f"{path}: {'.'.join(str(p) for p in err['loc']) or '(file)'}: {err['msg']}"
            for err in e.errors()
        ]
    return None


def _fields(parsed: BaseModel, record_type: type) -> dict[str, Any]:
    data = parsed.model_dump(mode="python")
    return {f.name: data[f.name] for f in fields(record_type)}


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _to_json(record: ContextRecord | ToolRecord) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return convert(asdict(record))
