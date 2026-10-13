"""Convert the small provider intent DTO into a validated metric Operation."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from sidewit.domain.context import ContextDefinition
from sidewit.domain.dialect import Dialect
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding, Period
from sidewit.domain.operation import SqlOperation
from sidewit.domain.params import value_matches_type
from sidewit.generation.clarification import LLMClarification
from sidewit.generation.metric_plan import LLMMetricPlan
from sidewit.generation.parameters import convert_parameter
from sidewit.validation.metric_contract import canonical_ref, metric_template, period_bounds


@dataclass
class CompiledMetric:
    operation: SqlOperation | None = None
    binding: MetricBinding | None = None
    errors: list[Issue] | None = None
    clarifications: list[LLMClarification] | None = None


def compile_plan(
    plan: LLMMetricPlan,
    definition: ContextDefinition,
    reference_time: datetime,
    timezone: str,
    *,
    dialect: Dialect,
    metric_id: str | None = None,
    date_field: str | None = None,
    period_override: Period | None = None,
) -> CompiledMetric:
    """`definition` is a saved, integrity-checked Context definition."""

    def invalid(message: str, ref: str | None = None) -> CompiledMetric:
        return CompiledMetric(errors=[Issue(code="metric_plan_invalid", message=message, ref=ref)])

    metric = next((m for m in definition.metrics if m.id == plan.metric_id), None)
    if metric is None or (metric_id is not None and plan.metric_id != metric_id):
        return invalid("Select the registered metric required by this request.", plan.metric_id)
    expected = {p.name: p.type for p in metric.inputs}
    values: dict[str, str | int | float | bool] = {}
    for param in plan.parameters:
        if param.name in values or expected.get(param.name) != param.type:
            return invalid("Input names and types must match the metric exactly.", param.name)
        value = convert_parameter(param.value, param.type)
        if not value_matches_type(value, param.type):
            return invalid("Input value has an invalid type or format.", param.name)
        values[param.name] = value
    if set(values) != set(expected):
        return invalid("All metric inputs are required; extra inputs are forbidden.")
    try:
        period = period_override or (
            Period.model_validate(plan.period.model_dump()) if plan.period else None
        )
    except ValidationError:
        return invalid("Invalid period; rolling days must be between 1 and 3660.")
    resolved: str | None = None
    if period is not None:
        # Text interpretation belongs to the planner. Never infer a choice from a mention:
        # a field can be negated, offered as an alternative or requested only in the output.
        resolved = date_field if date_field is not None else plan.date_field
        if resolved is None:
            return (
                CompiledMetric(
                    clarifications=[
                        LLMClarification(
                            question="Choose a date field for this period.",
                            options=metric.time_fields,
                        )
                    ]
                )
                if metric.time_fields
                else invalid("This metric does not support a period.")
            )
        if resolved.lower() not in {ref.lower() for ref in metric.time_fields}:
            return invalid("Date field must be one of the selected metric's time_fields.", resolved)
        try:
            resolved = canonical_ref(definition, resolved)
            statement, types = metric_template(definition, metric, resolved, dialect)
            values["start_at"], values["end_at"] = period_bounds(
                period, reference_time, timezone, types["start_at"]
            )
        except (ValueError, OverflowError):
            return invalid("Period or time field is incompatible with this metric.")
    else:
        statement, types = metric_template(definition, metric, None, dialect)
    return CompiledMetric(
        operation=SqlOperation(
            dialect=dialect, statement=statement, parameters=values, parameter_types=types
        ),
        binding=MetricBinding(metric_id=metric.id, time_field=resolved),
    )
