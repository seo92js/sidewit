"""Developer-owned SQL metric contracts. No SQL supplied by the model lives here."""

import math
import re
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from sidewit.domain.issue import Issue
from sidewit.domain.params import ParamType, matches_allowed_value, value_matches_type

if TYPE_CHECKING:
    from sidewit.domain.context import ContextDefinition, ContextField

Comparison = Literal["=", "!=", ">", ">=", "<", "<="]
Scalar = StrictStr | StrictInt | StrictFloat | StrictBool
MetricId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
# Period bounds are server parameters; metric inputs may not use these names.
RESERVED_INPUTS = frozenset({"start_at", "end_at"})


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MetricAggregate(ContractModel):
    kind: Literal["row_count", "non_null_count", "sum", "avg", "min", "max"]
    field: str | None = None
    distinct: bool = False


class MetricJoin(ContractModel):
    from_: str = Field(alias="from")
    to: str


class FixedCondition(ContractModel):
    """A business constant as data: `field op value`, e.g. rentals.state = 'SETTLED'. Used by
    metric fixed_filters and by Business Rule conditions; the server enforces it by field and
    value, never by matching rule prose."""

    field: str
    op: Comparison = "="
    value: Scalar


class MetricInput(ContractModel):
    name: MetricId
    type: ParamType
    field: str
    op: Comparison = "="
    description: str | None = None


class MetricDefinition(ContractModel):
    id: MetricId
    description: str = Field(min_length=1)
    resource: str
    aggregate: MetricAggregate
    joins: list[MetricJoin] = []
    group_by: list[str] = []
    fixed_filters: list[FixedCondition] = []
    inputs: list[MetricInput] = []
    time_fields: list[str] = []
    default_time_field: str | None = None


class MetricBinding(ContractModel):
    metric_id: MetricId
    time_field: str | None = None


class Period(ContractModel):
    kind: Literal["rolling_days", "previous_day", "previous_week", "previous_month"]
    days: Annotated[int, Field(strict=True, ge=1, le=3660)] | None = None


def field_for(definition: "ContextDefinition", ref: str) -> "ContextField | None":
    resource, _, name = ref.partition(".")
    table = definition.find_resource(resource)
    return (
        next((f for f in table.fields if f.name.lower() == name.lower()), None) if table else None
    )


def field_type(field: "ContextField") -> ParamType | None:
    """Deliberately small PostgreSQL scalar type set; unknown types fail closed, except an
    enum-like field (user-defined type) whose allowed values are all strings."""
    base = re.sub(r"\(.*?\)", "", field.type.lower()).strip()
    known = _known_type(base)
    if (
        known is None
        and field.allowed_values
        and all(isinstance(v, str) for v in field.allowed_values)
    ):
        return "string"
    return known


def _known_type(base: str) -> ParamType | None:
    if base == "date":
        return "date"
    if base in {
        "timestamp",
        "timestamp with time zone",
        "timestamp without time zone",
        "timestamptz",
    }:
        return "date-time"
    if base in {
        "int",
        "integer",
        "smallint",
        "bigint",
        "int2",
        "int4",
        "int8",
        "serial",
        "bigserial",
    }:
        return "integer"
    if base in {"numeric", "decimal", "real", "float", "float4", "float8", "double precision"}:
        return "number"
    if base in {"bool", "boolean"}:
        return "boolean"
    if base in {"text", "varchar", "char", "character", "character varying", "uuid"}:
        return "string"
    return None


def metric_issues(definition: "ContextDefinition") -> list[Issue]:
    """Integrity of every registered contract. Each issue's ref points at the offending part,
    e.g. `regional_revenue.inputs.region` or `regional_revenue.aggregate.field`."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for metric in definition.metrics:
        if metric.id in seen:
            issues.append(_issue(metric, "", "Metric id is defined more than once."))
        seen.add(metric.id)
        available = _joined_resources(definition, metric, issues)
        _check_references(definition, metric, available, issues)
        _check_aggregate(definition, metric, issues)
        _check_inputs(definition, metric, issues)
        _check_fixed_filters(definition, metric, issues)
        _check_time_fields(definition, metric, issues)
    return issues


def _issue(metric: MetricDefinition, part: str, message: str) -> Issue:
    ref = f"{metric.id}.{part}" if part else metric.id
    return Issue(code="invalid_metric_contract", message=message, ref=ref)


def _joined_resources(
    definition: "ContextDefinition", metric: MetricDefinition, issues: list[Issue]
) -> set[str]:
    """Resources reachable through the contract's joins, each adding one new resource."""
    if not definition.find_resource(metric.resource):
        issues.append(_issue(metric, "resource", "Metric resource does not exist."))
    declared = {frozenset((r.from_.lower(), r.to.lower())) for r in definition.relations}
    available = {metric.resource.lower()}
    for index, join in enumerate(metric.joins):
        pair = (join.from_.lower(), join.to.lower())
        left, right = (ref.partition(".")[0] for ref in pair)
        if frozenset(pair) not in declared or (left in available) == (right in available):
            issues.append(
                _issue(
                    metric,
                    f"joins[{index}]",
                    "Each inner join must add one resource through a declared Context relation.",
                )
            )
        available.update((left, right))
    return available


def _check_references(
    definition: "ContextDefinition",
    metric: MetricDefinition,
    available: set[str],
    issues: list[Issue],
) -> None:
    refs = [("group_by", r) for r in metric.group_by]
    refs += [("time_fields", r) for r in metric.time_fields]
    refs += [("fixed_filters", f.field) for f in metric.fixed_filters]
    refs += [(f"inputs.{p.name}", p.field) for p in metric.inputs]
    if metric.aggregate.field:
        refs.append(("aggregate.field", metric.aggregate.field))
    for part, ref in refs:
        if not definition.has_field(ref) or ref.partition(".")[0].lower() not in available:
            issues.append(_issue(metric, part, f"Field '{ref}' is unavailable in this metric."))
    for part, refs_list in (("group_by", metric.group_by), ("time_fields", metric.time_fields)):
        if len({r.lower() for r in refs_list}) != len(refs_list):
            issues.append(_issue(metric, part, f"{part} must not contain duplicate fields."))


def _check_aggregate(
    definition: "ContextDefinition", metric: MetricDefinition, issues: list[Issue]
) -> None:
    aggregate = metric.aggregate
    if aggregate.kind == "row_count":
        if aggregate.field is not None or aggregate.distinct:
            issues.append(
                _issue(
                    metric,
                    "aggregate",
                    "row_count uses COUNT(*) and accepts neither field nor distinct.",
                )
            )
    elif not aggregate.field:
        issues.append(_issue(metric, "aggregate.field", "This aggregate requires a field."))
    if aggregate.distinct and aggregate.kind != "non_null_count":
        issues.append(
            _issue(metric, "aggregate.distinct", "distinct is supported only for non_null_count.")
        )
    field = field_for(definition, aggregate.field) if aggregate.field else None
    if field is None:
        return
    kind = field_type(field)
    if aggregate.kind in {"sum", "avg"} and kind not in {"integer", "number"}:
        issues.append(
            _issue(metric, "aggregate.field", "sum/avg requires a known numeric field type.")
        )
    if aggregate.kind in {"min", "max"} and (
        kind in {None, "boolean"} or field.type.strip().lower() == "uuid"
    ):
        issues.append(
            _issue(
                metric,
                "aggregate.field",
                "min/max requires a supported numeric, text, date or timestamp field.",
            )
        )


def _check_inputs(
    definition: "ContextDefinition", metric: MetricDefinition, issues: list[Issue]
) -> None:
    fixed_fields = {f.field.lower() for f in metric.fixed_filters}
    names: set[str] = set()
    for param in metric.inputs:
        part = f"inputs.{param.name}"
        if param.name in names or param.name in RESERVED_INPUTS:
            issues.append(
                _issue(metric, part, "Input names must be unique and may not use start_at/end_at.")
            )
        names.add(param.name)
        if param.field.lower() in fixed_fields:
            issues.append(_issue(metric, part, "A fixed filter field cannot also be an input."))
        field = field_for(definition, param.field)
        if field is not None:
            kind = field_type(field)
            if not (kind == param.type or (kind == "integer" and param.type == "number")):
                issues.append(_issue(metric, part, "Input type must match its known field type."))


def _check_fixed_filters(
    definition: "ContextDefinition", metric: MetricDefinition, issues: list[Issue]
) -> None:
    for index, fixed in enumerate(metric.fixed_filters):
        if (problem := fixed_value_problem(definition, fixed)) is not None:
            issues.append(_issue(metric, f"fixed_filters[{index}]", problem))


def fixed_value_problem(definition: "ContextDefinition", condition: FixedCondition) -> str | None:
    """Why a fixed condition's value cannot match its field, or None. An unknown field is
    reported by the caller's reference check."""
    if isinstance(condition.value, float) and not math.isfinite(condition.value):
        return "Fixed numeric values must be finite."
    if isinstance(condition.value, str) and not condition.value.strip():
        return "Fixed value must not be empty."
    field = field_for(definition, condition.field)
    if field is None:
        return None
    kind = field_type(field)
    if kind is None or not value_matches_type(condition.value, kind):
        return "Fixed value must match its known field type."
    if field.allowed_values and not any(
        matches_allowed_value(condition.value, a) for a in field.allowed_values
    ):
        return f"Fixed value {condition.value!r} is not one of the field's allowed values."
    return None


def _check_time_fields(
    definition: "ContextDefinition", metric: MetricDefinition, issues: list[Issue]
) -> None:
    for ref in metric.time_fields:
        field = field_for(definition, ref)
        if field is not None and field_type(field) not in {"date", "date-time"}:
            issues.append(
                _issue(metric, "time_fields", f"'{ref}' must be a date or timestamp field.")
            )
    if metric.default_time_field and metric.default_time_field.lower() not in {
        ref.lower() for ref in metric.time_fields
    }:
        issues.append(
            _issue(metric, "default_time_field", "default_time_field must belong to time_fields.")
        )
