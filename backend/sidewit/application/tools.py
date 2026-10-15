"""Deterministic saved Tool invocation shared by REST and MCP."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sidewit.domain.operation import SqlOperation
from sidewit.errors import SidewitError
from sidewit.store import ContextRecord, Store, ToolRecord
from sidewit.tools.service import InvalidArguments, ToolDisabled, ToolInvalid, invoke
from sidewit.validation.sql_validator import ValidationResult


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


class InvokeResult(BaseModel):
    tool_id: uuid.UUID
    validation: ValidationResult
    tool: str
    tool_version: int
    context_version: int
    operation: SqlOperation


def get_context(store: Store, context_id: uuid.UUID) -> ContextRecord:
    context = store.context(context_id)
    if context is None:
        raise SidewitError(404, "context_not_found", f"Context '{context_id}' does not exist.")
    return context


def get_tool(store: Store, tool_id: uuid.UUID) -> ToolRecord:
    tool = store.tool_by_id(tool_id)
    if tool is None:
        raise SidewitError(404, "tool_not_found", f"Tool '{tool_id}' does not exist.")
    return tool


def check_tool_version(tool: ToolRecord, expected: int) -> None:
    if tool.version != expected:
        raise SidewitError(
            409,
            "version_conflict",
            "Tool was modified by someone else.",
            {"current_version": tool.version, "expected_version": expected},
        )


def invoke_tool(tool_id: uuid.UUID, body: InvokeRequest, store: Store) -> InvokeResult:
    tool = get_tool(store, tool_id)
    context = get_context(store, tool.context_id)
    if body.expected_version is not None:
        check_tool_version(tool, body.expected_version)
    try:
        operation, validation = invoke(store, tool, context, body.arguments)
    except ToolDisabled as e:
        raise SidewitError(
            409, "tool_disabled", "Tool is disabled. Enable it before invoking."
        ) from e
    except ToolInvalid as e:
        raise SidewitError(
            409,
            "tool_invalid",
            "Tool is incompatible with the current context.",
            {"validation_errors": [i.model_dump() for i in e.errors]},
        ) from e
    except InvalidArguments as e:
        raise SidewitError(
            422,
            "invalid_arguments",
            "Arguments do not match the tool's input_schema.",
            {"errors": [i.model_dump() for i in e.errors]},
        ) from e
    return InvokeResult(
        tool_id=tool.id,
        validation=validation,
        tool=tool.name,
        tool_version=tool.version,
        context_version=tool.context_version,
        operation=operation,
    )
