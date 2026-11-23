"""File storage for Contexts and Tools (SPEC §11). No database.

Records live in memory and are written through to one JSON file each:

    <data_dir>/contexts/<context_id>.json
    <data_dir>/tools/<tool_id>.json

Concurrency model: one process owns the directory. Every mutation runs synchronously on the
event loop (no `await` between reading the current state and writing the file), so a request
never observes or overwrites another request's half-done change. This replaces database row
locks. Each file is replaced atomically (write a temporary file, fsync, rename).

Stored records are immutable snapshots shared by `*_snapshot`, `definition` and `tool_check`;
records are frozen and their dict fields must not be modified either; derive changes with
`dataclasses.replace`.
"""

import errno
import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
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
from sidewit.observability import emit
from sidewit.validation.input_schema import input_schema_issues
from sidewit.validation.sql_validator import ValidationResult


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ToolRecord:
    context_id: uuid.UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    operation: dict[str, Any]
    context_version: int
    status: str
    enabled: bool = True
    version: int = 1
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    source_generation_id: uuid.UUID | None = None
    source_step_id: str | None = None
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


class StoreUnavailable(StoreError):
    """Recovery failed; nothing is served until a later retry reloads the files."""

    def __init__(self) -> None:
        super().__init__("Storage recovery failed", operation="access")


# A failed recovery is retried at most this often, so a broken directory is not re-read per
# request while a transient error still heals without a restart.
RESTORE_INTERVAL_SECONDS = 5.0


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
    enabled: bool = Field(default=True, strict=True)
    validation_errors: list[Issue]
    validation: ValidationResult | None
    source_generation_id: uuid.UUID | None
    source_step_id: str | None = Field(default=None, pattern=r"^step_[1-5]$")
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
        self._definitions: dict[uuid.UUID, ContextDefinition] = {}
        self._tool_names: dict[uuid.UUID, str] = {}
        # Existing name-based files keep their path; renaming changes one file atomically.
        self._tool_paths: dict[uuid.UUID, Path] = {}
        # Structural verdicts belong to this snapshot; actual invoke values are always checked.
        self._tool_checks: OrderedDict[tuple[uuid.UUID, int, uuid.UUID, int], ValidationResult] = (
            OrderedDict()
        )
        self._available = False
        self._next_restore = 0.0
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
            if isinstance(e, BlockingIOError):
                message = "Data directory is already owned by another process"
            else:
                message = f"Data directory {root} is not usable by UID {os.getuid()}: {e.strerror}"
            raise StoreError(message, operation="open") from e

    def close(self) -> None:
        self._available = False
        self._tool_checks.clear()
        self._definitions.clear()
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def restore_due(self) -> bool:
        return not self._available and time.monotonic() >= self._next_restore

    def require_available(self) -> None:
        if not self._available:
            raise StoreUnavailable()

    # read-only snapshots (shared) -----------------------------------------------------

    def context_snapshot(self, context_id: uuid.UUID) -> ContextRecord | None:
        return self.contexts.get(context_id)

    def tool_snapshot(self, tool_id: uuid.UUID) -> ToolRecord | None:
        name = self._tool_names.get(tool_id)
        return self.tools.get(name) if name is not None else None

    def tool_snapshots(self, context_id: uuid.UUID) -> list[ToolRecord]:
        return sorted(
            (t for t in self.tools.values() if t.context_id == context_id), key=lambda t: t.name
        )

    def definition(self, context: ContextRecord) -> ContextDefinition:
        """Parsed `context.definition`, cached only for the currently stored snapshot."""
        if self.contexts.get(context.id) is not context:
            return ContextDefinition.model_validate(context.definition)
        parsed = self._definitions.get(context.id)
        if parsed is None:
            parsed = ContextDefinition.model_validate(context.definition)
            self._definitions[context.id] = parsed
        return parsed

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
        return result

    def remember_tool_check(
        self, tool: ToolRecord, context: ContextRecord, result: ValidationResult
    ) -> None:
        key = (tool.id, tool.version, context.id, context.version)
        self._tool_checks[key] = deepcopy(result)
        self._tool_checks.move_to_end(key)
        while len(self._tool_checks) > 1024:
            self._tool_checks.popitem(last=False)

    # writes ---------------------------------------------------------------------------

    def save_context(self, record: ContextRecord) -> ContextRecord:
        self.require_available()
        for key in list(self._tool_checks):
            if key[2] == record.id:
                del self._tool_checks[key]
        self._definitions.pop(record.id, None)
        stored = deepcopy(replace(record, updated_at=_now()))
        self._write(self._contexts_dir / f"{record.id}.json", stored)
        self.contexts[record.id] = stored
        return stored

    def save_tool(self, record: ToolRecord) -> ToolRecord:
        self.require_available()
        occupied = self.tools.get(record.name)
        if occupied is not None and occupied.id != record.id:
            raise StoreError("Tool name is already in use.", operation="write")
        previous_name = self._tool_names.get(record.id)
        path = self._tool_paths.get(record.id, self._tools_dir / f"{record.id}.json")
        self._forget_tool_checks(record.id)
        stored = deepcopy(replace(record, updated_at=_now()))
        self._write(path, stored)
        if previous_name is not None and previous_name != record.name:
            self.tools.pop(previous_name)
        self.tools[record.name] = stored
        self._tool_names[record.id] = record.name
        self._tool_paths[record.id] = path
        return stored

    def delete_context(self, record: ContextRecord) -> None:
        self.require_available()
        self._unlink(self._contexts_dir / f"{record.id}.json")
        self.contexts.pop(record.id, None)
        self._definitions.pop(record.id, None)

    def delete_tool(self, record: ToolRecord) -> None:
        self.require_available()
        self._forget_tool_checks(record.id)
        self._unlink(self._tool_paths[record.id])
        self.tools.pop(record.name, None)
        self._tool_names.pop(record.id, None)
        self._tool_paths.pop(record.id, None)

    def _forget_tool_checks(self, tool_id: uuid.UUID) -> None:
        for key in list(self._tool_checks):
            if key[0] == tool_id:
                del self._tool_checks[key]

    def restore(self) -> None:
        """Reload memory from disk after a failed write left a record changed in memory."""
        self._next_restore = time.monotonic() + RESTORE_INTERVAL_SECONDS
        self._available = False
        self._tool_checks.clear()
        self._load()

    # files ----------------------------------------------------------------------------

    def _write(self, path: Path, record: ContextRecord | ToolRecord) -> None:
        try:
            data = json.dumps(
                _to_json(record), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            encoded = (data + "\n").encode("utf-8")
        except (ValueError, TypeError) as e:
            raise StoreError("Record contains unsupported JSON values.", operation="write") from e
        try:
            fd, temporary = _create_temporary(path)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(encoded)
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
        definitions: dict[uuid.UUID, ContextDefinition] = {}
        tools: dict[str, ToolRecord] = {}
        tool_paths: dict[uuid.UUID, Path] = {}
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
                record = ContextRecord(
                    **(_fields(parsed, ContextRecord) | {"definition": parsed.definition.to_json()})
                )
                contexts[record.id] = record
                definitions[record.id] = parsed.definition
        for path in _json_files(self._tools_dir):
            parsed = _parse(path, _ToolFile, problems)
            if parsed is None:
                continue
            if path.stem != str(parsed.id) and re.fullmatch(TOOL_NAME_PATTERN, path.stem) is None:
                problems.append(f"{path}: file name must be the Tool id or a legacy Tool name")
            elif parsed.context_id not in contexts:
                problems.append(f"{path}: Context {parsed.context_id} does not exist")
            elif parsed.id in tool_paths:
                problems.append(f"{path}: duplicate Tool id {parsed.id}")
            elif parsed.name in tools:
                problems.append(f"{path}: duplicate Tool name '{parsed.name}'")
            else:
                tools[parsed.name] = ToolRecord(**_fields(parsed, ToolRecord))
                tool_paths[parsed.id] = path
        if problems:
            raise StoreError(
                "Invalid data files:\n" + "\n".join(f"  {p}" for p in problems),
                operation="validate",
            )
        self.contexts, self.tools = contexts, tools
        self._definitions = definitions
        self._tool_names = {t.id: t.name for t in tools.values()}
        self._tool_paths = tool_paths
        self._available = True
        emit("store_loaded", contexts=len(self.contexts), tools=len(self.tools))


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


def _create_temporary(path: Path) -> tuple[int, Path]:
    """Like mkstemp, but with normal file permissions (umask, or the replaced file's mode)
    instead of 0600, so backups can read the data."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    for _ in range(100):
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}"
        try:
            fd = os.open(temporary, flags, 0o666)
        except FileExistsError:
            continue
        if mode is not None:
            try:
                os.fchmod(fd, mode)
            except BaseException:
                os.close(fd)
                temporary.unlink(missing_ok=True)
                raise
        return fd, temporary
    raise FileExistsError(errno.EEXIST, "No unused temporary file name")


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
