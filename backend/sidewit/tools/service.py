"""Tools: validated, reusable, deterministic operation interfaces (SPEC §3.3, §5.2, §5.4).

Nothing here calls an LLM, executes SQL or connects to an application database.
"""

import uuid
from typing import Any

from pydantic import BaseModel

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding
from sidewit.domain.operation import SqlOperation
from sidewit.domain.params import matches_allowed_value
from sidewit.generation.history import GenerationRecord
from sidewit.store import ContextRecord, Store, ToolRecord
from sidewit.validation.input_schema import (
    argument_issues,
    input_schema_for,
    input_schema_issues,
    parameter_types,
)
from sidewit.validation.metric_contract import binding_issues, metric_template
from sidewit.validation.sql_validator import (
    DEFAULT_POLICY,
    ValidationResult,
    allowed_values_by_parameter,
    analyze_sql_operation,
    parameter_value_issues,
)


class ToolDisabled(Exception):
    pass


class ToolInvalid(Exception):
    def __init__(self, errors: list[Issue]) -> None:
        self.errors = errors


class InvalidArguments(Exception):
    def __init__(self, errors: list[Issue]) -> None:
        self.errors = errors


class ContractUnavailable(Exception):
    """The Tool is not bound to a metric, or its metric no longer exists."""


class InvalidTool(BaseModel):
    id: uuid.UUID
    name: str
    errors: list[Issue]


class Revalidation(BaseModel):
    checked: int = 0
    valid: list[str] = []
    invalid: list[InvalidTool] = []


def validate_tool(
    context: ContextRecord,
    input_schema: Any,
    statement: str,
    metric_binding: MetricBinding | None = None,
) -> ValidationResult:
    """input_schema contract (§4.1) + structural SQL validation with DEFAULT_POLICY, so a
    Tool may use NOW() etc. The schema's inputs are the declared parameters, which makes
    undeclared_parameter/unused_parameter enforce placeholders == inputs."""
    schema_errors = input_schema_issues(input_schema)
    if schema_errors:
        result = ValidationResult(
            valid=False, errors=schema_errors, warnings=[], checks=["input_schema"]
        )
        return result
    definition = ContextDefinition.model_validate(context.definition)
    declared = parameter_types(input_schema)
    analysis = analyze_sql_operation(
        statement, context.dialect, definition, declared, None, DEFAULT_POLICY
    )
    sql = analysis.result
    errors = list(sql.errors)
    if sql.valid:
        for name, values in analysis.parameter_allowed.items():
            enum = input_schema["properties"].get(name, {}).get("enum", [])
            if not values or any(
                not any(matches_allowed_value(v, a) for a in values) for v in enum
            ):
                errors.append(
                    Issue(
                        code="input_enum_conflict",
                        message=f"Input '{name}' conflicts with the Context's allowed values.",
                        ref=name,
                    )
                )
    if metric_binding is not None:
        errors += binding_issues(definition, metric_binding, statement, declared, context.dialect)
    result = ValidationResult(
        valid=not errors,
        errors=errors,
        warnings=sql.warnings,
        checks=["input_schema", *sql.checks, *(["metric_contract"] if metric_binding else [])],
    )
    return result


def tool_binding(tool: ToolRecord) -> MetricBinding | None:
    return MetricBinding.model_validate(tool.metric_binding) if tool.metric_binding else None


def save_validated_tool(
    store: Store, tool: ToolRecord, context: ContextRecord, result: ValidationResult
) -> None:
    """Persist one validation outcome, then cache it against the same Tool/Context versions."""
    tool.validation = result.model_dump(mode="json")
    if result.valid:
        tool.status = "valid"
        tool.context_version = context.version
        tool.validation_errors = []
    else:
        # context_version keeps the last version the tool was valid against (§3.3).
        tool.status = "invalid"
        tool.validation_errors = [e.model_dump() for e in result.errors]
    store.save_tool(tool)
    store.remember_tool_check(tool, context, result)


def revalidate_context_tools(store: Store, context: ContextRecord) -> Revalidation:
    """Rechecks and saves every Tool of `context`. Runs without awaiting, in the same
    synchronous step as the Context save (store concurrency model)."""
    summary = Revalidation()
    for tool in store.tools_of(context.id):
        result = validate_tool(
            context, tool.input_schema, tool.operation["statement"], tool_binding(tool)
        )
        save_validated_tool(store, tool, context, result)
        summary.checked += 1
        if result.valid:
            summary.valid.append(tool.name)
        else:
            summary.invalid.append(InvalidTool(id=tool.id, name=tool.name, errors=result.errors))
    return summary


def effective_status(tool: ToolRecord, context: ContextRecord) -> str:
    """A stored verdict belongs to the Context version it was checked against."""
    return (
        "stale"
        if tool.status == "valid" and tool.context_version != context.version
        else tool.status
    )


def is_current(tool: ToolRecord, context: ContextRecord) -> bool:
    return tool.status == "valid" and tool.context_version == context.version


def invoke(
    store: Store, tool: ToolRecord, context: ContextRecord, arguments: dict[str, Any]
) -> tuple[SqlOperation, ValidationResult]:
    if not tool.enabled:
        raise ToolDisabled("Tool is disabled.")
    result = store.tool_check(tool, context)
    if result is None:
        result = validate_tool(
            context, tool.input_schema, tool.operation["statement"], tool_binding(tool)
        )
        if not is_current(tool, context) or not result.valid:
            save_validated_tool(store, tool, context, result)
        else:
            store.remember_tool_check(tool, context, result)
    if not result.valid:
        raise ToolInvalid(result.errors)

    issues = argument_issues(tool.input_schema, arguments)
    if issues:
        raise InvalidArguments(issues)
    # A cached structural verdict cannot decide values after CAST or other wrappers.
    # Reuse the validator with actual inputs; no PostgreSQL planning or LLM call here.
    issues = parameter_value_issues(
        tool.operation["statement"],
        context.dialect,
        ContextDefinition.model_validate(context.definition),
        parameter_types(tool.input_schema),
        arguments,
    )
    if issues:
        raise InvalidArguments(issues)
    # Input order is contractual; file serialization sorts object keys.
    order = tool.input_schema["required"]
    types = parameter_types(tool.input_schema)
    return SqlOperation(
        dialect=context.dialect,
        statement=tool.operation["statement"],
        parameters={name: arguments[name] for name in order},
        parameter_types={name: types[name] for name in order},
    ), result


def draft_from_generation(generation: GenerationRecord, context: ContextRecord) -> dict[str, Any]:
    """Draft material only: not validated and not trusted. Saving re-validates it. String
    inputs compared with an enum-like field accept only that field's allowed values."""
    operation = generation.operation or {}
    suggestion = generation.tool_suggestion or {}
    schema = input_schema_for(operation.get("parameter_types", {}))
    if operation.get("statement"):
        _restrict_to_allowed_values(
            schema,
            allowed_values_by_parameter(
                operation["statement"],
                context.dialect,
                ContextDefinition.model_validate(context.definition),
            ),
        )
    return {
        "context_id": generation.context_id,
        "name": suggestion.get("name"),
        "description": suggestion.get("description", ""),
        "input_schema": schema,
        "operation": {"type": "sql", "statement": operation.get("statement", "")},
        "example_arguments": operation.get("parameters", {}),
        "source_generation_id": generation.id,
        "metric_binding": generation.metric_binding,
    }


def contract_snapshot(tool: ToolRecord, context: ContextRecord) -> tuple[str, dict[str, Any]]:
    """SQL and input_schema rebuilt from the Tool's metric contract in the current Context.
    String inputs on enum-like fields take the Context's allowed values; descriptions (and a
    hand-set enum, when the field has no allowed values) are kept where the type is unchanged."""
    if tool.metric_binding is None:
        raise ContractUnavailable("This tool is not bound to a metric contract.")
    binding = MetricBinding.model_validate(tool.metric_binding)
    definition = ContextDefinition.model_validate(context.definition)
    metric = next((m for m in definition.metrics if m.id == binding.metric_id), None)
    if metric is None:
        raise ContractUnavailable(f"Metric '{binding.metric_id}' is no longer registered.")
    try:
        statement, types = metric_template(
            definition,
            metric,
            binding.time_field,
            context.dialect,
        )
    except ValueError as e:
        raise ContractUnavailable(
            "The Tool's bound date field is no longer supported. Restore it in the "
            "Context or create a new Tool with a supported date basis."
        ) from e
    schema = input_schema_for(types)
    _restrict_to_allowed_values(
        schema, allowed_values_by_parameter(statement, context.dialect, definition)
    )
    previous = tool.input_schema.get("properties", {})
    previous_types = parameter_types(tool.input_schema)
    for name, prop in schema["properties"].items():
        if previous_types.get(name) == types[name]:
            old = previous.get(name, {})
            if "description" in old:
                prop["description"] = old["description"]
            # The Context's allowed values win; a hand-set enum is kept only without them.
            if "enum" in old and "enum" not in prop:
                prop["enum"] = old["enum"]
    return statement, schema


def _restrict_to_allowed_values(schema: dict[str, Any], allowed: dict[str, list[Any]]) -> None:
    """Sets `enum` on string inputs bound to an enum-like field (input_schema allows string
    enums only)."""
    for name, prop in schema["properties"].items():
        values = allowed.get(name)
        if prop.get("type") == "string" and "format" not in prop and values:
            if all(isinstance(v, str) for v in values):
                prop["enum"] = list(values)
