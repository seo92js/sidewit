"""Saved Tool catalog and deterministic validation of each selected call."""

import json
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from sidewit.domain.operation import SqlOperation
from sidewit.generation.clarification import CLARIFICATION_INSTRUCTIONS
from sidewit.generation.dto import LLMParameter
from sidewit.generation.parameters import convert_parameter
from sidewit.generation.planners import GenerationInput
from sidewit.store import ContextRecord, Store, ToolRecord
from sidewit.tools.service import InvalidArguments, ToolInvalid, effective_status, invoke
from sidewit.validation.input_schema import parameter_types
from sidewit.validation.sql_validator import ValidationResult

MAX_CATALOG_CHARS = 100_000


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    parameters: list[LLMParameter]


class ToolSelectionError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


def selection_context(
    candidates: list[ToolRecord], context: ContextRecord, request: GenerationInput
) -> str:
    """Bounded catalog and local reference time for plan selection."""
    catalog = json.dumps(
        [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "operation": t.operation,
                "status": effective_status(t, context),
            }
            for t in candidates
        ],
        ensure_ascii=False,
    )
    if len(catalog) > MAX_CATALOG_CHARS:
        raise ToolSelectionError(
            422, "tool_catalog_too_large", "Tool catalog exceeds selection limits."
        )
    local_time = request.reference_time.astimezone(ZoneInfo(context.timezone)).isoformat()
    return (
        f"\nReference time: {local_time}\nTimezone: {context.timezone}"
        f"\nContext: {json.dumps(context.definition, ensure_ascii=False)}"
        f"\nTool catalog: {catalog}" + CLARIFICATION_INSTRUCTIONS
    )


def invoke_selection(
    selection: ToolCall,
    candidates: list[ToolRecord],
    store: Store,
    context: ContextRecord,
) -> tuple[SqlOperation, ValidationResult, ToolRecord]:
    selected = next((t for t in candidates if t.name == selection.tool_name), None)
    if selected is None:
        raise ToolSelectionError(
            422, "invalid_tool_selection", "Selected Tool is not in this Context."
        )
    current = store.tool(selected.name)
    if current is None or (current.id, current.version) != (selected.id, selected.version):
        raise ToolSelectionError(409, "tool_changed", "Selected Tool changed. Start a new request.")
    types = parameter_types(selected.input_schema)
    arguments = {}
    for parameter in selection.parameters:
        kind = types.get(parameter.name)
        if parameter.name in arguments or parameter.type != kind:
            raise ToolSelectionError(
                422,
                "invalid_tool_arguments",
                "Selected Tool arguments have invalid names or types.",
            )
        arguments[parameter.name] = convert_parameter(parameter.value, parameter.type)
    try:
        operation, validation = invoke(store, current, context, arguments)
    except ToolInvalid as exc:
        raise ToolSelectionError(
            409,
            "tool_invalid",
            "Selected Tool is incompatible with the current Context.",
            {
                "tool": current.name,
                "validation_errors": [issue.model_dump() for issue in exc.errors],
            },
        ) from exc
    except InvalidArguments as exc:
        raise ToolSelectionError(
            422,
            "invalid_tool_arguments",
            "Selected Tool arguments do not satisfy its contract.",
            {"tool": current.name, "errors": [issue.model_dump() for issue in exc.errors]},
        ) from exc
    return operation, validation, current
