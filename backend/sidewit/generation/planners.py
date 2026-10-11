"""Generate paths behind one interface (SPEC §6).

- SqlPlanner: the model writes parameterized SQL (Context without registered metrics).
- MetricPlanner: the model picks a registered metric, its inputs and a period; the server
  compiles the SQL from the developer-owned contract (metrics only).
- HybridPlanner: one call picks the route — a registered metric when one answers the whole
  question, otherwise model-written SQL that applies the metrics' definitions as rules.

A planner owns its prompt, provider DTO and conversion into a domain Operation. The provider
DTOs never leave this module and `service`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from sidewit.domain.context import ContextDefinition
from sidewit.domain.dialect import Dialect
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding, Period
from sidewit.domain.operation import SqlOperation
from sidewit.generation.dto import LLMGenerationOutput, LLMToolSuggestion
from sidewit.generation.metric_compiler import compile_plan
from sidewit.generation.metric_plan import (
    METRIC_PROMPT_VERSION,
    LLMHybridPlan,
    LLMMetricPlan,
    metric_prompt,
)
from sidewit.generation.parameters import convert_parameter
from sidewit.generation.prompt import (
    HYBRID_PROMPT_VERSION,
    PROMPT_VERSION,
    render_hybrid_prompt,
    render_repair_message,
    render_system_prompt,
)
from sidewit.validation.sql_validator import (
    CONTRACT_POLICY,
    ValidationResult,
    generate_policy,
    validate_sql_operation,
)


class InvalidGenerateRequest(Exception):
    """Structured request options that cannot apply to this Context (422 before any LLM call)."""


@dataclass(frozen=True)
class MetricOptions:
    """Caller choices that pin the request to registered metrics; each overrides the model.
    Any of them (or `require_metric`) disables model-written SQL for this request."""

    metric_id: str | None = None
    date_field: str | None = None
    period: Period | None = None
    require_metric: bool = False

    def any(self) -> bool:
        return (
            self.require_metric
            or self.metric_id is not None
            or self.date_field is not None
            or self.period is not None
        )


@dataclass(frozen=True)
class GenerationInput:
    question: str
    reference_time: datetime
    options: MetricOptions = field(default_factory=MetricOptions)


@dataclass
class Plan:
    """Result of converting an `ok` model output."""

    kind: Literal["ok", "ambiguous", "invalid"]
    operation: SqlOperation | None = None
    validation: ValidationResult | None = None
    binding: MetricBinding | None = None
    reason: str | None = None
    clarifications: list[str] = field(default_factory=list)


class StatusOutput(Protocol):
    """Fields every planner DTO shares; the service handles non-ok answers through these."""

    status: Literal["ok", "ambiguous", "unanswerable"]
    reason: str | None
    clarifications: list[str]
    tool_suggestion: LLMToolSuggestion | None

    def model_dump_json(self) -> str: ...


class Planner[O: BaseModel](Protocol):
    """`O` is the planner's provider DTO: requested from the provider as `output_schema`
    and passed back to `plan` for `ok` answers."""

    prompt_version: str
    output_schema: type[O]

    def system_prompt(self) -> str: ...
    def plan(self, output: O) -> Plan: ...
    def repair_message(self, errors: list[Issue]) -> str: ...


def planner_for(
    definition: ContextDefinition,
    allow_generated_sql: bool,
    dialect: Dialect,
    timezone: str,
    request: GenerationInput,
) -> Planner[Any]:
    """Registered metrics first; model-written SQL only when the Context and the request allow
    it. Raises InvalidGenerateRequest for options that cannot apply."""
    args = (definition, dialect, timezone, request)
    if request.options.any():
        if not definition.metrics:
            raise InvalidGenerateRequest(
                "metric_id, date_field, period and require_metric need registered metrics."
            )
        _check_metric_options(definition, request.options)
        return MetricPlanner(*args)
    if not definition.metrics:
        return SqlPlanner(*args)
    return HybridPlanner(*args) if allow_generated_sql else MetricPlanner(*args)


def _check_metric_options(definition: ContextDefinition, options: MetricOptions) -> None:
    metric = None
    if options.metric_id is not None:
        metric = next((m for m in definition.metrics if m.id == options.metric_id), None)
        if metric is None:
            raise InvalidGenerateRequest("Choose a registered metric.")
    if options.date_field is not None and (
        metric is None
        or options.date_field.lower() not in {ref.lower() for ref in metric.time_fields}
    ):
        raise InvalidGenerateRequest("date_field requires a metric_id and one of its time_fields.")
    period = options.period
    if period is not None and (period.kind == "rolling_days") != (period.days is not None):
        raise InvalidGenerateRequest("Only rolling_days takes days.")


class _PlannerBase:
    """What every planner plans against: one Context, one request."""

    def __init__(
        self,
        definition: ContextDefinition,
        dialect: Dialect,
        timezone: str,
        request: GenerationInput,
    ) -> None:
        self.definition = definition
        self.dialect = dialect
        self.timezone = timezone
        self.request = request


class SqlPlanner(_PlannerBase):
    prompt_version = PROMPT_VERSION
    output_schema = LLMGenerationOutput

    def system_prompt(self) -> str:
        return render_system_prompt(
            self.definition, self.dialect, self.timezone, self.request.reference_time
        )

    def plan(self, output: LLMGenerationOutput) -> Plan:
        """Values that cannot be converted to their declared type are kept as-is for the
        Validator to reject; SQL is never rewritten."""
        parameters: dict[str, object] = {}
        parameter_types: dict[str, str] = {}
        duplicates: list[Issue] = []
        for param in output.parameters:
            if param.name in parameter_types:
                duplicates.append(
                    Issue(
                        code="duplicate_parameter",
                        message=f"Parameter '{param.name}' is listed more than once.",
                        ref=param.name,
                    )
                )
                continue
            parameters[param.name] = convert_parameter(param.value, param.type)
            parameter_types[param.name] = param.type

        operation = SqlOperation(
            dialect=self.dialect,
            statement=output.statement or "",
            parameters=parameters,
            parameter_types=parameter_types,
        )
        validation = validate_sql_operation(
            operation.statement,
            self.dialect,
            self.definition,
            operation.parameter_types,
            operation.parameters,
            generate_policy(self.request.question),
        )
        if duplicates:
            validation = validation.model_copy(
                update={"valid": False, "errors": duplicates + validation.errors}
            )
        return Plan(
            kind="ok" if validation.valid else "invalid",
            operation=operation,
            validation=validation,
        )

    def repair_message(self, errors: list[Issue]) -> str:
        return render_repair_message(errors)


class MetricPlanner(_PlannerBase):
    prompt_version = METRIC_PROMPT_VERSION
    output_schema = LLMMetricPlan

    def system_prompt(self) -> str:
        options = self.request.options
        return metric_prompt(
            self.definition,
            self.timezone,
            self.request.reference_time,
            options.metric_id,
            options.period,
        )

    def plan(self, output: LLMMetricPlan) -> Plan:
        options = self.request.options
        compiled = compile_plan(
            output,
            self.definition,
            self.request.question,
            self.request.reference_time,
            self.timezone,
            dialect=self.dialect,
            metric_id=options.metric_id,
            date_field=options.date_field,
            period_override=options.period,
        )
        if compiled.clarifications:
            return Plan(
                kind="ambiguous",
                reason="A date basis must be selected for this metric.",
                clarifications=compiled.clarifications,
            )
        if compiled.operation is None:
            validation = ValidationResult(
                valid=False, errors=compiled.errors or [], warnings=[], checks=["metric_contract"]
            )
            return Plan(kind="invalid", validation=validation, binding=compiled.binding)

        # The server built this SQL from the contract; re-check it structurally anyway. User
        # values are parameters by construction, so no request-literal check is needed.
        operation = compiled.operation
        validation = validate_sql_operation(
            operation.statement,
            self.dialect,
            self.definition,
            operation.parameter_types,
            operation.parameters,
            CONTRACT_POLICY,
        )
        validation = validation.model_copy(
            update={"checks": [*validation.checks, "metric_contract"]}
        )
        return Plan(
            kind="ok" if validation.valid else "invalid",
            operation=operation,
            validation=validation,
            binding=compiled.binding,
        )

    def repair_message(self, errors: list[Issue]) -> str:
        listed = "; ".join(f"{e.code}: {e.message}" for e in errors)
        return (
            "The metric plan failed validation. Return a corrected plan for the same "
            f"question; do not return SQL. Errors: {listed}"
        )


class HybridPlanner(_PlannerBase):
    """Routes each `ok` answer to the metric or SQL planner by the model's `route`. Both
    keep their own conversion and validation; a metric answer is contract-built SQL."""

    prompt_version = HYBRID_PROMPT_VERSION
    output_schema = LLMHybridPlan

    def __init__(
        self,
        definition: ContextDefinition,
        dialect: Dialect,
        timezone: str,
        request: GenerationInput,
    ) -> None:
        super().__init__(definition, dialect, timezone, request)
        self.sql = SqlPlanner(definition, dialect, timezone, request)
        self.metric = MetricPlanner(definition, dialect, timezone, request)
        self._last: SqlPlanner | MetricPlanner | None = None

    def system_prompt(self) -> str:
        return render_hybrid_prompt(
            self.definition, self.dialect, self.timezone, self.request.reference_time
        )

    def plan(self, output: LLMHybridPlan) -> Plan:
        common = {
            "status": "ok",
            "reason": None,
            "clarifications": [],
            "parameters": output.parameters,
            "tool_suggestion": output.tool_suggestion,
        }
        if output.route == "metric":
            self._last = self.metric
            return self.metric.plan(
                LLMMetricPlan(**common, metric_id=output.metric_id, period=output.period)
            )
        if output.route == "sql":
            self._last = self.sql
            return self.sql.plan(LLMGenerationOutput(**common, statement=output.statement))
        self._last = None
        issue = Issue(
            code="route_missing",
            message="An ok answer must set route to 'metric' or 'sql'.",
            ref="route",
        )
        return Plan(
            kind="invalid",
            validation=ValidationResult(valid=False, errors=[issue], warnings=[], checks=[]),
        )

    def repair_message(self, errors: list[Issue]) -> str:
        base = (
            self._last.repair_message(errors)
            if self._last is not None
            else "Your previous response failed validation: "
            + "; ".join(f"{e.code}: {e.message}" for e in errors)
        )
        return (
            base + " You may switch the route if a registered metric answers the whole "
            "question exactly, or if none does."
        )
