import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sidewit.api.errors import ApiError
from sidewit.api.state import HistoryDep, StoreDep
from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding
from sidewit.domain.operation import SqlOperation
from sidewit.domain.params import ParamType
from sidewit.domain.tool import TOOL_NAME_PATTERN, ToolOperation, ToolStatus
from sidewit.store import ContextRecord, Store, ToolRecord
from sidewit.tools.service import (
    ContractUnavailable,
    InvalidArguments,
    ToolInvalid,
    contract_snapshot,
    draft_from_generation,
    effective_status,
    invoke,
    save_validated_tool,
    tool_binding,
    validate_tool,
)
from sidewit.validation.sql_validator import (
    DEFAULT_POLICY,
    ValidationResult,
    validate_sql_operation,
)

router = APIRouter(tags=["tools"])


class ToolFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    # Raw JSON: checked by input_schema_issues so errors use the validation contract.
    input_schema: dict[str, Any]
    operation: ToolOperation

    @field_validator("description")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be blank")
        return value


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ToolUpdate(ToolFields):
    expected_version: int = Field(ge=1)


class ToolCreate(ToolFields):
    context_id: uuid.UUID
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    source_generation_id: uuid.UUID | None = None
    metric_binding: MetricBinding | None = None


class ToolOut(BaseModel):
    id: uuid.UUID
    context_id: uuid.UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    operation: ToolOperation
    version: int
    context_version: int
    status: ToolStatus
    validation_errors: list[Issue]
    validation: ValidationResult | None
    source_generation_id: uuid.UUID | None
    metric_binding: MetricBinding | None = None
    created_at: datetime
    updated_at: datetime


class ToolList(BaseModel):
    items: list[ToolOut]


class DraftRequest(BaseModel):
    generation_id: uuid.UUID


class ToolDraft(BaseModel):
    context_id: uuid.UUID
    name: str | None
    description: str
    input_schema: dict[str, Any]
    operation: ToolOperation
    example_arguments: dict[str, Any]
    source_generation_id: uuid.UUID
    metric_binding: MetricBinding | None = None


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


class InvokeResult(BaseModel):
    validation: ValidationResult
    tool: str
    tool_version: int
    context_version: int
    operation: SqlOperation


class ValidateOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sql"] = "sql"
    statement: str = Field(min_length=1, max_length=64000)
    parameter_types: dict[str, ParamType] = {}
    parameters: dict[str, Any] | None = None


class ValidateRequest(BaseModel):
    context_id: uuid.UUID
    operation: ValidateOperation
    input_schema: dict[str, Any] | None = None
    metric_binding: MetricBinding | None = None


def _out(tool: ToolRecord, store: Store) -> ToolOut:
    output = ToolOut.model_validate(tool, from_attributes=True)
    context = _context(store, tool.context_id)
    if effective_status(tool, context) == "stale":
        output.status = "stale"
        output.validation = None
        output.validation_errors = []
    return output


def _context(store: Store, context_id: uuid.UUID) -> ContextRecord:
    context = store.context(context_id)
    if context is None:
        raise ApiError(404, "context_not_found", f"Context '{context_id}' does not exist.")
    return context


def _tool(store: Store, name: str) -> ToolRecord:
    tool = store.tool(name)
    if tool is None:
        raise ApiError(404, "tool_not_found", f"Tool '{name}' does not exist.")
    return tool


def _check_version(tool: ToolRecord, expected: int) -> None:
    if tool.version != expected:
        raise ApiError(
            409,
            "version_conflict",
            "Tool was modified by someone else.",
            {"current_version": tool.version, "expected_version": expected},
        )


def _require_valid(result: ValidationResult) -> None:
    if not result.valid:
        raise ApiError(
            422,
            "tool_validation_failed",
            "Tool failed validation.",
            {
                "validation_errors": [e.model_dump() for e in result.errors],
                "warnings": [w.model_dump() for w in result.warnings],
            },
        )


@router.get("/tools")
async def list_tools(store: StoreDep, context_id: uuid.UUID | None = None) -> ToolList:
    tools = sorted(store.tools.values(), key=lambda t: t.name)
    if context_id is not None:
        tools = [t for t in tools if t.context_id == context_id]
    return ToolList(items=[_out(t, store) for t in tools])


@router.get("/tools/{name}")
async def get_tool(name: str, store: StoreDep) -> ToolOut:
    return _out(_tool(store, name), store)


@router.post("/tools/draft")
async def draft_tool(body: DraftRequest, store: StoreDep, history: HistoryDep) -> ToolDraft:
    generation = history.get(body.generation_id)
    if generation is None:
        raise ApiError(
            404,
            "generation_not_found",
            f"Generation '{body.generation_id}' is not in this server's recent history.",
        )
    if generation.status != "ok":
        raise ApiError(
            409,
            "generation_not_ok",
            f"Only generations with status 'ok' can be drafted (got '{generation.status}').",
        )
    context = _context(store, generation.context_id)
    return ToolDraft.model_validate(draft_from_generation(generation, context))


@router.post("/tools", status_code=status.HTTP_201_CREATED)
async def create_tool(body: ToolCreate, store: StoreDep, history: HistoryDep) -> ToolOut:
    context = _context(store, body.context_id)
    if store.tool(body.name) is not None:
        raise ApiError(409, "tool_name_conflict", f"Tool '{body.name}' already exists.")
    binding = body.metric_binding
    if body.source_generation_id is not None:
        generation = history.get(body.source_generation_id)
        if generation is None or generation.context_id != context.id or generation.status != "ok":
            raise ApiError(
                422,
                "invalid_source_generation",
                "source_generation_id must reference a recent ok generation of the same context.",
            )
        source_binding = (
            MetricBinding.model_validate(generation.metric_binding)
            if generation.metric_binding
            else None
        )
        if binding is not None and binding != source_binding:
            raise ApiError(
                422, "invalid_metric_binding", "Source metric binding cannot be replaced."
            )
        binding = source_binding
    validation = validate_tool(context, body.input_schema, body.operation.statement, binding)
    _require_valid(validation)

    tool = ToolRecord(
        context_id=context.id,
        name=body.name,
        description=body.description,
        input_schema=body.input_schema,
        operation=body.operation.model_dump(),
        version=1,
        context_version=context.version,
        status="valid",
        source_generation_id=body.source_generation_id,
        metric_binding=binding.model_dump() if binding else None,
    )
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.put("/tools/{name}")
async def update_tool(name: str, body: ToolUpdate, store: StoreDep) -> ToolOut:
    tool = _tool(store, name)
    context = _context(store, tool.context_id)
    _check_version(tool, body.expected_version)
    validation = validate_tool(
        context, body.input_schema, body.operation.statement, tool_binding(tool)
    )
    _require_valid(validation)

    operation = body.operation.model_dump()
    if (tool.description, tool.input_schema, tool.operation) != (
        body.description,
        body.input_schema,
        operation,
    ):
        tool.description = body.description
        tool.input_schema = body.input_schema
        tool.operation = operation
        tool.version += 1
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.post("/tools/{name}/refresh-contract")
async def refresh_contract(name: str, body: VersionRequest, store: StoreDep) -> ToolOut:
    """Rebuild a metric-bound Tool's SQL and inputs from its current contract. The Tool keeps
    its name, description and binding; the version increases when anything changes."""
    tool = _tool(store, name)
    context = _context(store, tool.context_id)
    _check_version(tool, body.expected_version)
    try:
        statement, input_schema = contract_snapshot(tool, context)
    except ContractUnavailable as e:
        raise ApiError(409, "metric_contract_unavailable", str(e)) from e
    validation = validate_tool(context, input_schema, statement, tool_binding(tool))
    _require_valid(validation)

    operation = {"type": "sql", "statement": statement}
    if (tool.input_schema, tool.operation) != (input_schema, operation):
        tool.input_schema = input_schema
        tool.operation = operation
        tool.version += 1
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.delete("/tools/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(name: str, store: StoreDep) -> Response:
    store.delete_tool(_tool(store, name))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tools/{name}/invoke")
async def invoke_tool(name: str, body: InvokeRequest, store: StoreDep) -> InvokeResult:
    tool = _tool(store, name)
    context = _context(store, tool.context_id)
    if body.expected_version is not None:
        _check_version(tool, body.expected_version)
    try:
        operation, validation = invoke(store, tool, context, body.arguments)
    except ToolInvalid as e:
        raise ApiError(
            409,
            "tool_invalid",
            "Tool is incompatible with the current context.",
            {"validation_errors": [i.model_dump() for i in e.errors]},
        ) from e
    except InvalidArguments as e:
        raise ApiError(
            422,
            "invalid_arguments",
            "Arguments do not match the tool's input_schema.",
            {"errors": [i.model_dump() for i in e.errors]},
        ) from e
    return InvokeResult(
        validation=validation,
        tool=tool.name,
        tool_version=tool.version,
        context_version=tool.context_version,
        operation=operation,
    )


@router.post("/validate")
async def validate(body: ValidateRequest, store: StoreDep) -> ValidationResult:
    """Dry-run validation for editors. Never saves anything."""
    context = _context(store, body.context_id)
    if body.input_schema is not None:
        if body.operation.parameter_types or body.operation.parameters is not None:
            raise ApiError(
                422,
                "invalid_validate_request",
                "With input_schema, the inputs come from the schema; omit "
                "operation.parameter_types and operation.parameters.",
            )
        return validate_tool(
            context, body.input_schema, body.operation.statement, body.metric_binding
        )
    if body.metric_binding is not None:
        raise ApiError(422, "invalid_metric_binding", "Metric validation requires input_schema.")
    return validate_sql_operation(
        body.operation.statement,
        context.dialect,
        ContextDefinition.model_validate(context.definition),
        body.operation.parameter_types,
        body.operation.parameters,
        DEFAULT_POLICY,
    )
