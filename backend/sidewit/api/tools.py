import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sidewit.api.state import HistoryDep, StoreDep
from sidewit.application.tools import (
    InvokeRequest,
    InvokeResult,
    check_tool_version,
    get_context,
    get_tool,
)
from sidewit.application.tools import (
    invoke_tool as run_invoke,
)
from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding
from sidewit.domain.params import ParamType
from sidewit.domain.tool import TOOL_NAME_PATTERN, ToolOperation, ToolStatus
from sidewit.errors import SidewitError
from sidewit.generation.history import GenerationRecord
from sidewit.generation.operations import operation_steps
from sidewit.store import Store, ToolRecord
from sidewit.tools.service import (
    ContractUnavailable,
    contract_snapshot,
    draft_from_generation,
    effective_status,
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
    name: str | None = Field(default=None, pattern=TOOL_NAME_PATTERN)


class ToolActivation(VersionRequest):
    enabled: bool = Field(strict=True)


class ToolCreate(ToolFields):
    enabled: bool = Field(default=True, strict=True)
    context_id: uuid.UUID
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    source_generation_id: uuid.UUID | None = None
    source_step_id: str | None = Field(default=None, pattern=r"^step_[1-5]$")
    metric_binding: MetricBinding | None = None

    @model_validator(mode="after")
    def _source(self):
        if self.source_step_id is not None and self.source_generation_id is None:
            raise ValueError("source_step_id requires source_generation_id.")
        return self


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
    enabled: bool
    validation_errors: list[Issue]
    validation: ValidationResult | None
    source_generation_id: uuid.UUID | None
    source_step_id: str | None = None
    metric_binding: MetricBinding | None = None
    created_at: datetime
    updated_at: datetime


class ToolList(BaseModel):
    items: list[ToolOut]


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation_id: uuid.UUID
    step_id: str | None = Field(default=None, pattern=r"^step_[1-5]$")


class ToolDraft(BaseModel):
    context_id: uuid.UUID
    name: str | None
    description: str
    input_schema: dict[str, Any]
    operation: ToolOperation
    example_arguments: dict[str, Any]
    source_generation_id: uuid.UUID
    source_step_id: str | None = None
    metric_binding: MetricBinding | None = None


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
    context = get_context(store, tool.context_id)
    if effective_status(tool, context) == "stale":
        output.status = "stale"
        output.validation = None
        output.validation_errors = []
    return output


def _require_valid(result: ValidationResult) -> None:
    if not result.valid:
        raise SidewitError(
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


@router.get("/tools/{tool_id}")
async def read_tool(tool_id: uuid.UUID, store: StoreDep) -> ToolOut:
    return _out(get_tool(store, tool_id), store)


@router.post("/tools/draft")
async def draft_tool(body: DraftRequest, store: StoreDep, history: HistoryDep) -> ToolDraft:
    generation = history.get(body.generation_id)
    if generation is None:
        raise SidewitError(
            404,
            "generation_not_found",
            f"Generation '{body.generation_id}' is not in this server's recent history.",
        )
    if generation.status != "ok":
        raise SidewitError(
            409,
            "generation_not_ok",
            f"Only generations with status 'ok' can be drafted (got '{generation.status}').",
        )
    step_id, source = _generation_source(generation, body.step_id)
    context = get_context(store, generation.context_id)
    draft = draft_from_generation(source, context)
    return ToolDraft.model_validate(draft | {"source_step_id": step_id})


def _generation_source(
    generation: GenerationRecord, step_id: str | None
) -> tuple[str, GenerationRecord]:
    steps = operation_steps(generation)
    if step_id is None:
        if len(steps) != 1:
            raise SidewitError(
                409, "plan_not_draftable", "Select a generated step to create a Tool draft."
            )
        step_id = steps[0]["id"]
    step = next((s for s in steps if s["id"] == step_id), None)
    if step is None:
        raise SidewitError(422, "invalid_source_step", "The selected step does not exist.")
    if step["source"] == "tool":
        raise SidewitError(409, "step_already_saved", "This step already references a saved Tool.")
    return step_id, replace(
        generation,
        operations=None,
        operation=step["operation"],
        validation=step["validation"],
        metric_binding=step.get("metric_binding"),
        tool_suggestion=step.get("tool_suggestion"),
    )


@router.post("/tools", status_code=status.HTTP_201_CREATED)
async def create_tool(body: ToolCreate, store: StoreDep, history: HistoryDep) -> ToolOut:
    context = get_context(store, body.context_id)
    if store.tool(body.name) is not None:
        raise SidewitError(409, "tool_name_conflict", f"Tool '{body.name}' already exists.")
    binding = body.metric_binding
    source_step_id = body.source_step_id
    if body.source_generation_id is not None:
        generation = history.get(body.source_generation_id)
        if generation is None or generation.context_id != context.id or generation.status != "ok":
            raise SidewitError(
                422,
                "invalid_source_generation",
                "source_generation_id must reference a recent ok generation of the same context.",
            )
        source_step_id, source = _generation_source(generation, body.source_step_id)
        source_binding = (
            MetricBinding.model_validate(source.metric_binding) if source.metric_binding else None
        )
        if binding is not None and binding != source_binding:
            raise SidewitError(
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
        enabled=body.enabled,
        source_generation_id=body.source_generation_id,
        source_step_id=source_step_id,
        metric_binding=binding.model_dump() if binding else None,
    )
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.put("/tools/{tool_id}")
async def update_tool(tool_id: uuid.UUID, body: ToolUpdate, store: StoreDep) -> ToolOut:
    tool = get_tool(store, tool_id)
    context = get_context(store, tool.context_id)
    check_tool_version(tool, body.expected_version)
    next_name = body.name if body.name is not None else tool.name
    if next_name != tool.name and store.tool(next_name) is not None:
        raise SidewitError(409, "tool_name_conflict", f"Tool '{next_name}' already exists.")
    validation = validate_tool(
        context, body.input_schema, body.operation.statement, tool_binding(tool)
    )
    _require_valid(validation)

    operation = body.operation.model_dump()
    if (tool.name, tool.description, tool.input_schema, tool.operation) != (
        next_name,
        body.description,
        body.input_schema,
        operation,
    ):
        tool.name = next_name
        tool.description = body.description
        tool.input_schema = body.input_schema
        tool.operation = operation
        tool.version += 1
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.patch("/tools/{tool_id}")
async def set_tool_enabled(tool_id: uuid.UUID, body: ToolActivation, store: StoreDep) -> ToolOut:
    tool = get_tool(store, tool_id)
    context = get_context(store, tool.context_id)
    check_tool_version(tool, body.expected_version)
    if tool.enabled == body.enabled:
        return _out(tool, store)
    if body.enabled:
        validation = validate_tool(
            context, tool.input_schema, tool.operation["statement"], tool_binding(tool)
        )
        _require_valid(validation)
        tool.enabled = True
        tool.version += 1
        save_validated_tool(store, tool, context, validation)
    else:
        # Disabling must remain possible even when the SQL is invalid or stale.
        tool.enabled = False
        tool.version += 1
        store.save_tool(tool)
    return _out(tool, store)


@router.post("/tools/{tool_id}/refresh-contract")
async def refresh_contract(tool_id: uuid.UUID, body: VersionRequest, store: StoreDep) -> ToolOut:
    """Rebuild a metric-bound Tool's SQL and inputs from its current contract. The Tool keeps
    its name, description and binding; the version increases when anything changes."""
    tool = get_tool(store, tool_id)
    context = get_context(store, tool.context_id)
    check_tool_version(tool, body.expected_version)
    try:
        statement, input_schema = contract_snapshot(tool, context)
    except ContractUnavailable as e:
        raise SidewitError(409, "metric_contract_unavailable", str(e)) from e
    validation = validate_tool(context, input_schema, statement, tool_binding(tool))
    _require_valid(validation)

    operation = {"type": "sql", "statement": statement}
    if (tool.input_schema, tool.operation) != (input_schema, operation):
        tool.input_schema = input_schema
        tool.operation = operation
        tool.version += 1
    save_validated_tool(store, tool, context, validation)
    return _out(tool, store)


@router.delete("/tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(tool_id: uuid.UUID, store: StoreDep) -> Response:
    store.delete_tool(get_tool(store, tool_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tools/{tool_id}/invoke")
async def invoke_tool(tool_id: uuid.UUID, body: InvokeRequest, store: StoreDep) -> InvokeResult:
    return run_invoke(tool_id, body, store)


@router.post("/validate")
async def validate(body: ValidateRequest, store: StoreDep) -> ValidationResult:
    """Dry-run validation for editors. Never saves anything."""
    context = get_context(store, body.context_id)
    if body.input_schema is not None:
        if body.operation.parameter_types or body.operation.parameters is not None:
            raise SidewitError(
                422,
                "invalid_validate_request",
                "With input_schema, the inputs come from the schema; omit "
                "operation.parameter_types and operation.parameters.",
            )
        return validate_tool(
            context, body.input_schema, body.operation.statement, body.metric_binding
        )
    if body.metric_binding is not None:
        raise SidewitError(
            422, "invalid_metric_binding", "Metric validation requires input_schema."
        )
    return validate_sql_operation(
        body.operation.statement,
        context.dialect,
        ContextDefinition.model_validate(context.definition),
        body.operation.parameter_types,
        body.operation.parameters,
        DEFAULT_POLICY,
    )
