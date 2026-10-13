"""Golden suite format (SPEC §9): `context.json` + `cases.yaml` in one directory."""

import json
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from sidewit.domain.context import ContextDefinition, definition_issues, generation_issues
from sidewit.domain.dialect import Dialect
from sidewit.domain.params import ParamType

Op = Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in"]
Construct = Literal["select_star", "relative_time_function"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstraintExpectation(_Strict):
    """A comparison on `field` in WHERE/HAVING/JOIN ON. `op` may list alternatives.
    At most one of `value` (SQL literal), `parameter_value` (bound parameter value) or
    `parameter` (any parameter) narrows the other side."""

    field: str
    op: Op | list[Op]
    value: str | int | float | bool | None = None
    parameter_value: Any = None
    parameter: bool = False

    @field_validator("op", mode="before")
    @classmethod
    def normalize_ops(cls, value):
        def normalize(op):
            return "!=" if op == "<>" else op.lower() if isinstance(op, str) else op

        return [normalize(op) for op in value] if isinstance(value, list) else normalize(value)

    @model_validator(mode="after")
    def validate_target(self):
        if not self.ops():
            raise ValueError("op must contain at least one operator")
        if sum((self.value is not None, self.parameter_value is not None, self.parameter)) > 1:
            raise ValueError("use only one of value, parameter_value or parameter")
        return self

    def ops(self) -> list[str]:
        return self.op if isinstance(self.op, list) else [self.op]


class ParameterExpectation(_Strict):
    value: Any
    type: ParamType | None = None


class AggregateExpectation(_Strict):
    function: Literal["count", "sum", "avg", "min", "max"]
    field: str
    distinct: bool = False


class OperationExpectations(_Strict):
    resources: list[str] | None = None  # exact set of base resources
    fields: list[str] = []  # must be referenced
    forbidden_fields: list[str] = []
    joins: list[tuple[str, str]] = []  # equality joins, either direction
    aggregates: list[AggregateExpectation] = []
    group_by: list[str] = []
    constraints: list[ConstraintExpectation] = []
    parameters: list[ParameterExpectation] = []  # matched by value (names are free)
    forbidden_parameter_values: list[Any] = []  # e.g. Business Rule constants
    forbidden_literals: list[str] = []  # e.g. user values that must be parameters
    forbidden_constructs: list[Construct] = []


class Expectations(OperationExpectations):
    status: Literal["ok", "ambiguous", "unanswerable"]
    source: Literal["metric", "generated_sql", "plan"] | None = None
    steps: list[OperationExpectations] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_steps(self):
        if self.steps is not None:
            if self.status != "ok" or self.source != "plan":
                raise ValueError("steps require status ok and source plan.")
            if self.model_fields_set & OperationExpectations.model_fields.keys():
                raise ValueError("Put SQL expectations inside steps when evaluating a plan.")
        return self


class ReferenceAnswer(_Strict):
    """A correct answer written by the case author. Never used for scoring: tests check
    that it satisfies the expectations, which proves the case is satisfiable."""

    statement: str
    parameters: dict[str, Any] = {}
    parameter_types: dict[str, ParamType] = {}


class GoldenCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    question: str = Field(min_length=1)
    category: str
    reference_time: AwareDatetime | None = None
    expect: Expectations
    reference: ReferenceAnswer | None = None


class SuiteContext(_Strict):
    name: str = Field(min_length=1)
    dialect: Dialect = "postgresql"
    timezone: str = "UTC"
    allow_generated_sql: bool = True
    definition: ContextDefinition

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown IANA timezone: {value}") from e
        return value

    @model_validator(mode="after")
    def validate_definition(self):
        if issues := definition_issues(self.definition) + generation_issues(
            self.definition, self.allow_generated_sql
        ):
            raise ValueError("; ".join(issue.message for issue in issues))
        return self


class CasesFile(_Strict):
    reference_time: AwareDatetime
    cases: list[GoldenCase] = Field(min_length=1)


class Suite(BaseModel):
    name: str
    context: SuiteContext
    reference_time: AwareDatetime
    cases: list[GoldenCase]


def load_suite(directory: str | Path) -> Suite:
    path = Path(directory)
    context = SuiteContext.model_validate(json.loads((path / "context.json").read_text()))
    cases = CasesFile.model_validate(yaml.safe_load((path / "cases.yaml").read_text()))
    ids = [c.id for c in cases.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate case ids: {', '.join(duplicates)}")
    return Suite(
        name=path.name,
        context=context,
        reference_time=cases.reference_time,
        cases=cases.cases,
    )
