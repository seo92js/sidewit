import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from sidewit.api.errors import ApiError
from sidewit.api.state import HistoryDep, StoreDep
from sidewit.ddl.importer import DdlParseError, ImportMode, PendingForeignKey, import_ddl
from sidewit.domain.context import (
    ContextDefinition,
    check_context_size,
    definition_issues,
    generation_issues,
)
from sidewit.domain.dialect import Dialect
from sidewit.domain.issue import Issue
from sidewit.store import ContextRecord, Store
from sidewit.tools.service import Revalidation, revalidate_context_tools

router = APIRouter(prefix="/contexts", tags=["contexts"])


class ContextBody(BaseModel):
    name: str = Field(min_length=1)
    dialect: Dialect
    timezone: str = "UTC"
    # Metrics are always tried first; this allows model-written SQL when none fits.
    allow_generated_sql: bool = True
    definition: ContextDefinition = ContextDefinition()

    @model_validator(mode="after")
    def _bounded_definition(self):
        check_context_size(self.definition)
        return self

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown IANA timezone: {value}") from e
        return value


class ContextUpdate(ContextBody):
    # Required on update so that omitting a field never resets it to its default.
    timezone: str
    allow_generated_sql: bool
    definition: ContextDefinition
    expected_version: int


class ContextOut(BaseModel):
    id: uuid.UUID
    name: str
    dialect: Dialect
    timezone: str
    allow_generated_sql: bool
    definition: ContextDefinition
    version: int
    created_at: datetime
    updated_at: datetime


class ContextList(BaseModel):
    items: list[ContextOut]


class ContextUpdateResult(BaseModel):
    context: ContextOut
    revalidation: Revalidation


class ImportDdlRequest(BaseModel):
    ddl: str = Field(min_length=1, max_length=256000)
    mode: ImportMode = "merge"
    expected_version: int | None = Field(default=None, ge=1)
    # UI previews merge against the current unsaved draft; callers omitting it use storage.
    base_definition: ContextDefinition | None = None
    pending_foreign_keys: list[PendingForeignKey] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _bounded_base(self):
        if self.base_definition is not None:
            if self.expected_version is None:
                raise ValueError("expected_version is required with base_definition")
            check_context_size(self.base_definition)
        return self


class ImportDdlResult(BaseModel):
    context_version: int
    definition: ContextDefinition
    warnings: list[str]
    warning_details: list[Issue]
    pending_foreign_keys: list[PendingForeignKey]


def _out(record: ContextRecord) -> ContextOut:
    return ContextOut(
        id=record.id,
        name=record.name,
        dialect=record.dialect,
        timezone=record.timezone,
        allow_generated_sql=record.allow_generated_sql,
        definition=ContextDefinition.model_validate(record.definition),
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _check_definition(definition: ContextDefinition, allow_generated_sql: bool) -> None:
    issues = definition_issues(definition) + generation_issues(definition, allow_generated_sql)
    if issues:
        raise ApiError(
            422,
            "invalid_context_definition",
            "Context definition failed integrity checks.",
            {"errors": [i.model_dump() for i in issues]},
        )


def _name_conflict(name: str) -> ApiError:
    return ApiError(409, "context_name_conflict", f"A context named '{name}' already exists.")


def _get_or_404(store: Store, context_id: uuid.UUID) -> ContextRecord:
    record = store.context(context_id)
    if record is None:
        raise ApiError(404, "context_not_found", f"Context '{context_id}' does not exist.")
    return record


def _check_name(store: Store, name: str, own_id: uuid.UUID | None = None) -> None:
    other = store.context_named(name)
    if other is not None and other.id != own_id:
        raise _name_conflict(name)


@router.get("")
async def list_contexts(store: StoreDep) -> ContextList:
    records = sorted(store.contexts.values(), key=lambda r: r.name)
    return ContextList(items=[_out(r) for r in records])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_context(body: ContextBody, store: StoreDep) -> ContextOut:
    _check_definition(body.definition, body.allow_generated_sql)
    _check_name(store, body.name)
    record = ContextRecord(
        name=body.name,
        dialect=body.dialect,
        timezone=body.timezone,
        allow_generated_sql=body.allow_generated_sql,
        definition=body.definition.to_json(),
    )
    store.save_context(record)
    return _out(record)


@router.get("/{context_id}")
async def get_context(context_id: uuid.UUID, store: StoreDep) -> ContextOut:
    return _out(_get_or_404(store, context_id))


@router.put("/{context_id}")
async def update_context(
    context_id: uuid.UUID, body: ContextUpdate, store: StoreDep
) -> ContextUpdateResult:
    record = _get_or_404(store, context_id)
    if record.version != body.expected_version:
        raise ApiError(
            409,
            "version_conflict",
            "Context was modified by someone else.",
            {"current_version": record.version, "expected_version": body.expected_version},
        )
    _check_definition(body.definition, body.allow_generated_sql)
    _check_name(store, body.name, record.id)

    definition = body.definition.to_json()
    changed = (
        record.name != body.name
        or record.dialect != body.dialect
        or record.timezone != body.timezone
        or record.allow_generated_sql != body.allow_generated_sql
        or ContextDefinition.model_validate(record.definition).to_json() != definition
    )
    if changed:
        record.name = body.name
        record.dialect = body.dialect
        record.timezone = body.timezone
        record.allow_generated_sql = body.allow_generated_sql
        record.definition = definition
        record.version += 1
    if not changed:
        return ContextUpdateResult(context=_out(record), revalidation=Revalidation())
    # Persist the version first. Interrupted Tool writes then leave older versions, which
    # must be revalidated against this persisted Context before invocation.
    store.save_context(record)
    revalidation = revalidate_context_tools(store, record)
    return ContextUpdateResult(context=_out(record), revalidation=revalidation)


@router.delete("/{context_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_context(context_id: uuid.UUID, store: StoreDep, history: HistoryDep) -> Response:
    record = _get_or_404(store, context_id)
    names = [t.name for t in store.tools_of(context_id)]
    if names:
        raise ApiError(
            409,
            "context_has_tools",
            "Delete the tools of this context first.",
            {"tools": names},
        )
    store.delete_context(record)
    history.forget_context(context_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{context_id}/import-ddl")
async def import_context_ddl(
    context_id: uuid.UUID, body: ImportDdlRequest, store: StoreDep
) -> ImportDdlResult:
    record = _get_or_404(store, context_id)
    if body.expected_version is not None and record.version != body.expected_version:
        raise ApiError(
            409,
            "version_conflict",
            "Context changed before the DDL preview was created.",
            {"current_version": record.version, "expected_version": body.expected_version},
        )
    existing = body.base_definition or ContextDefinition.model_validate(record.definition)
    try:
        result = import_ddl(
            existing, body.ddl, record.dialect, body.mode, body.pending_foreign_keys
        )
        check_context_size(result.definition)
    except (DdlParseError, ValueError) as e:
        raise ApiError(422, "ddl_parse_error", "Could not parse DDL.", {"reason": str(e)}) from e
    return ImportDdlResult(
        context_version=record.version,
        definition=result.definition,
        warnings=result.warnings,
        warning_details=result.warning_details,
        pending_foreign_keys=result.pending_foreign_keys,
    )
