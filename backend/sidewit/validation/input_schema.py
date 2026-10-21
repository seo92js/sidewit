"""Tool input contract (SPEC §4.1): the restricted JSON Schema subset and argument checks.

v0.1: a flat object, every property required, no default/optional/array.
"""

from typing import Any

from sidewit.domain.issue import Issue
from sidewit.domain.params import (
    PARAM_NAME_PATTERN,
    ParamType,
    valid_json_unicode,
    value_matches_type,
)

_TOP_LEVEL_KEYS = {"type", "properties", "required", "additionalProperties"}
_PROPERTY_KEYS = {"type", "format", "description", "enum"}
_JSON_TYPES = {"string", "integer", "number", "boolean"}
_FORMATS = {"date", "date-time"}


def input_schema_issues(schema: Any) -> list[Issue]:
    issues: list[Issue] = []

    def bad(message: str, ref: str | None = None) -> None:
        issues.append(Issue(code="invalid_input_schema", message=message, ref=ref))

    if not isinstance(schema, dict):
        bad("input_schema must be a JSON object.")
        return issues
    if not valid_json_unicode(schema):
        bad("input_schema strings must contain valid Unicode.")
        return issues
    for key in sorted(schema.keys() - _TOP_LEVEL_KEYS):
        bad(f"Keyword '{key}' is not supported at the top level.", key)
    if schema.get("type") != "object":
        bad("input_schema type must be 'object'.", "type")
    if schema.get("additionalProperties") is not False:
        bad("additionalProperties must be false.", "additionalProperties")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        bad("properties must be an object.", "properties")
        properties = {}
    for name, prop in properties.items():
        _property_issues(name, prop, bad)

    required = schema.get("required")
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        bad("required must be a list of property names.", "required")
    else:
        if len(required) != len(set(required)):
            bad("required must not repeat names.", "required")
        missing = [n for n in properties if n not in required]
        unknown = [n for n in required if n not in properties]
        if missing:
            bad(
                "Every property must be required in v0.1 (no optional inputs): "
                + ", ".join(missing),
                "required",
            )
        if unknown:
            bad("required lists names that are not properties: " + ", ".join(unknown), "required")
    return issues


def _property_issues(name: str, prop: Any, bad) -> None:
    ref = f"properties.{name}"
    if not PARAM_NAME_PATTERN.match(name):
        bad(f"Input name '{name}' must match {PARAM_NAME_PATTERN.pattern}.", ref)
    if not isinstance(prop, dict):
        bad(f"Input '{name}' must be an object.", ref)
        return
    for key in sorted(prop.keys() - _PROPERTY_KEYS):
        reason = "defaults are not supported in v0.1" if key == "default" else "not supported"
        bad(f"Keyword '{key}' on input '{name}' is {reason}.", f"{ref}.{key}")

    json_type = prop.get("type")
    if not isinstance(json_type, str) or json_type not in _JSON_TYPES:
        bad(
            f"Input '{name}' type must be one of {', '.join(sorted(_JSON_TYPES))}.",
            f"{ref}.type",
        )
    if "format" in prop and (
        json_type != "string"
        or not isinstance(prop["format"], str)
        or prop["format"] not in _FORMATS
    ):
        bad(f"Input '{name}' format must be 'date' or 'date-time' on a string.", f"{ref}.format")
    if "description" in prop and not isinstance(prop["description"], str):
        bad(f"Input '{name}' description must be a string.", f"{ref}.description")
    if "enum" in prop:
        enum = prop["enum"]
        if (
            json_type != "string"
            or not isinstance(enum, list)
            or not enum
            or not all(isinstance(v, str) for v in enum)
            or len(enum) != len(set(enum))
        ):
            bad(
                f"Input '{name}' enum must be a non-empty list of unique strings on a string.",
                f"{ref}.enum",
            )


def parameter_types(schema: dict[str, Any]) -> dict[str, ParamType]:
    """Maps each property with a recognizable type to its parameter type. Properties with an
    invalid type are omitted (input_schema_issues reports them)."""
    types: dict[str, ParamType] = {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return types
    for name, prop in properties.items():
        if (
            not isinstance(prop, dict)
            or not isinstance(prop.get("type"), str)
            or prop["type"] not in _JSON_TYPES
        ):
            continue
        if (
            prop["type"] == "string"
            and isinstance(prop.get("format"), str)
            and prop["format"] in _FORMATS
        ):
            types[name] = prop["format"]
        else:
            types[name] = prop["type"]
    return types


def input_schema_for(types: dict[str, ParamType]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, param_type in types.items():
        if param_type in _FORMATS:
            properties[name] = {"type": "string", "format": param_type}
        else:
            properties[name] = {"type": param_type}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def argument_issues(schema: dict[str, Any], arguments: dict[str, Any]) -> list[Issue]:
    """Checks invoke arguments against a valid input_schema."""
    if not valid_json_unicode(arguments):
        return [
            Issue(code="argument_type_mismatch", message="Arguments must contain valid Unicode.")
        ]
    issues: list[Issue] = []
    properties: dict[str, Any] = schema["properties"]
    types = parameter_types(schema)
    for name in properties:
        if name not in arguments:
            issues.append(
                Issue(code="missing_argument", message=f"Argument '{name}' is required.", ref=name)
            )
    for name in arguments:
        if name not in properties:
            issues.append(
                Issue(
                    code="unexpected_argument",
                    message=f"Argument '{name}' is not an input of this tool.",
                    ref=name,
                )
            )
    for name, value in arguments.items():
        if name not in properties:
            continue
        if not value_matches_type(value, types[name]):
            issues.append(
                Issue(
                    code="argument_type_mismatch",
                    message=f"Argument '{name}' must be a valid {types[name]}.",
                    ref=name,
                )
            )
        elif "enum" in properties[name] and value not in properties[name]["enum"]:
            issues.append(
                Issue(
                    code="argument_not_in_enum",
                    message=f"Argument '{name}' must be one of: "
                    + ", ".join(properties[name]["enum"]),
                    ref=name,
                )
            )
    return issues
