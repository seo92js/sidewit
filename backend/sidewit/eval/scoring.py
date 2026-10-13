"""Deterministic scoring of one generation against a golden case. No LLM, no SQL strings."""

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from sidewit.domain.context import ContextDefinition
from sidewit.domain.operation import SqlOperation
from sidewit.eval.analysis import AnalysisError, SqlFacts, analyze
from sidewit.eval.cases import ConstraintExpectation, Expectations
from sidewit.validation.sql_validator import (
    CONTRACT_POLICY,
    DEFAULT_POLICY,
    GENERATE_POLICY,
    validate_sql_operation,
)


class Failure(BaseModel):
    expectation: str
    detail: str


def score_operation(
    expect: Expectations,
    operation: SqlOperation,
    context: ContextDefinition,
    timezone: str,
    source: str | None = None,
) -> list[Failure]:
    """Failures of an `ok` operation against `expect` (empty list: the case passes).
    `source` is the API's provenance: SQL compiled from a metric contract is re-checked with
    the policy Generate used for it."""
    validation = validate_sql_operation(
        operation.statement,
        operation.dialect,
        context,
        operation.parameter_types,
        operation.parameters,
        {"metric": CONTRACT_POLICY, "tool": DEFAULT_POLICY}.get(source, GENERATE_POLICY),
    )
    failures = [
        Failure(expectation="validity", detail=f"{error.code}: {error.message}")
        for error in validation.errors
    ]
    try:
        facts = analyze(operation.statement, operation.dialect, context)
    except AnalysisError as e:
        return failures + [Failure(expectation="analysis", detail=str(e))]

    tz = ZoneInfo(timezone)
    values = operation.parameters

    def fail(expectation: str, detail: str) -> None:
        failures.append(Failure(expectation=expectation, detail=detail))

    if expect.resources is not None:
        expected = {r.lower() for r in expect.resources}
        if facts.resources != expected:
            fail(
                "resources",
                f"expected exactly {sorted(expected)}, referenced {sorted(facts.resources)}",
            )
    for ref in expect.fields:
        if ref.lower() not in facts.fields:
            fail("fields", f"{ref} is not referenced")
    for ref in expect.forbidden_fields:
        if ref.lower() in facts.fields:
            fail("forbidden_fields", f"{ref} must not be used")
    for a, b in expect.joins:
        if frozenset((a.lower(), b.lower())) not in facts.joins:
            fail("joins", f"no join {a} = {b}")
    for aggregate in expect.aggregates:
        if (
            aggregate.function,
            aggregate.field.lower(),
            aggregate.distinct,
        ) not in facts.aggregates:
            prefix = "DISTINCT " if aggregate.distinct else ""
            fail("aggregates", f"no {aggregate.function.upper()}({prefix}{aggregate.field})")
    for ref in expect.group_by:
        if ref.lower() not in facts.group_by:
            fail("group_by", f"{ref} is not grouped")
    for constraint in expect.constraints:
        if not _has_constraint(constraint, facts, values, tz):
            fail("constraints", _describe(constraint))
    for param in expect.parameters:
        if not any(
            _same(param.value, actual, tz)
            and (param.type is None or operation.parameter_types.get(name) == param.type)
            for name, actual in values.items()
        ):
            typed = f" ({param.type})" if param.type else ""
            fail("parameters", f"no parameter with value {param.value!r}{typed}")
    for forbidden in expect.forbidden_parameter_values:
        names = [n for n, v in values.items() if _same(forbidden, v, tz)]
        if names:
            fail(
                "forbidden_parameter_values",
                f"{forbidden!r} must be a SQL literal, not parameter :{names[0]}",
            )
    for literal in expect.forbidden_literals:
        if any(_same(literal, actual, tz) for actual in facts.literals):
            fail("forbidden_literals", f"{literal!r} must be a parameter, not a SQL literal")
    for construct in expect.forbidden_constructs:
        if construct in facts.constructs:
            fail("forbidden_constructs", f"{construct} is not allowed")
    return failures


def _has_constraint(
    expected: ConstraintExpectation, facts: SqlFacts, values: dict[str, Any], tz: ZoneInfo
) -> bool:
    for comparison in facts.comparisons:
        if comparison.field != expected.field.lower() or comparison.op not in expected.ops():
            continue
        if expected.value is not None:
            if any(_same(expected.value, literal, tz) for literal in comparison.literals):
                return True
        elif expected.parameter_value is not None:
            if comparison.parameter in values and _same(
                expected.parameter_value, values[comparison.parameter], tz
            ):
                return True
        elif expected.parameter:
            if comparison.parameter in values:
                return True
        else:
            return True
    return False


def _describe(constraint: ConstraintExpectation) -> str:
    ops = " or ".join(constraint.ops())
    if constraint.value is not None:
        target = repr(constraint.value)
    elif constraint.parameter_value is not None:
        target = f"a parameter bound to {constraint.parameter_value!r}"
    elif constraint.parameter:
        target = "a parameter"
    else:
        target = "anything"
    return f"no condition {constraint.field} {ops} {target}"


def _same(expected: Any, actual: Any, tz: ZoneInfo) -> bool:
    """Equality that ignores representation: instants for dates/date-times (a bare date is
    midnight in the context timezone), numeric value for numbers, otherwise exact."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if expected is None or actual is None:
        return expected is actual
    expected_instant, actual_instant = _instant(expected, tz), _instant(actual, tz)
    if expected_instant is not None or actual_instant is not None:
        return expected_instant == actual_instant
    expected_number, actual_number = _number(expected), _number(actual)
    if expected_number is not None and actual_number is not None:
        return expected_number == actual_number
    return str(expected) == str(actual)


def _instant(value: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or len(value) < 10 or value[4] != "-":
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time(), tz)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | str):
        try:
            number = Decimal(str(value))
            return number if number.is_finite() else None
        except InvalidOperation:
            return None
    return None
