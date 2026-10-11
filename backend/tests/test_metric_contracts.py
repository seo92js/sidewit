import copy
from datetime import datetime

import pytest
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from sidewit.ddl.importer import import_ddl
from sidewit.domain.context import ContextDefinition, definition_issues
from sidewit.domain.metric import MetricBinding, MetricInput, Period
from sidewit.generation.dto import LLMParameter
from sidewit.generation.metric_compiler import compile_plan
from sidewit.generation.metric_plan import LLMMetricPlan, LLMPeriod
from sidewit.validation.metric_contract import binding_issues as _binding_issues
from sidewit.validation.metric_contract import period_bounds

DEFINITION = {
    "resources": [
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "bigint", "nullable": True},
                {"name": "customer_id", "type": "bigint"},
                {"name": "amount", "type": "numeric"},
                {"name": "status", "type": "text"},
                {"name": "paid_at", "type": "timestamptz"},
                {"name": "ordered_at", "type": "timestamptz"},
            ],
        },
        {
            "name": "customers",
            "fields": [
                {"name": "id", "type": "bigint"},
                {"name": "region", "type": "text"},
            ],
        },
    ],
    "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
    "metrics": [
        {
            "id": "orders_count",
            "description": "Order rows",
            "resource": "orders",
            "aggregate": {"kind": "row_count"},
            "time_fields": ["orders.paid_at", "orders.ordered_at"],
        },
        {
            "id": "regional_revenue",
            "description": "Paid revenue in a region",
            "resource": "orders",
            "aggregate": {"kind": "sum", "field": "orders.amount"},
            "joins": [{"from": "orders.customer_id", "to": "customers.id"}],
            "fixed_filters": [{"field": "orders.status", "value": "COMPLETED"}],
            "inputs": [{"name": "region", "type": "string", "field": "customers.region"}],
            "time_fields": ["orders.paid_at", "orders.ordered_at"],
            "default_time_field": "orders.paid_at",
        },
    ],
}
REFERENCE = datetime.fromisoformat("2026-10-02T09:00:00+09:00")


def binding_issues(definition, binding, statement, types):
    return _binding_issues(definition, binding, statement, types, "postgresql")


def definition():
    return ContextDefinition.model_validate(copy.deepcopy(DEFINITION))


def plan(metric_id="orders_count", parameters=(), period=None):
    return LLMMetricPlan(
        status="ok",
        reason=None,
        clarifications=[],
        metric_id=metric_id,
        parameters=list(parameters),
        period=period,
        tool_suggestion=None,
    )


def compile_(output=None, context=None, question="All order rows", **kwargs):
    return compile_plan(
        output or plan(),
        context or definition(),
        question,
        REFERENCE,
        "Asia/Seoul",
        dialect="postgresql",
        **kwargs,
    )


def test_nullable_count_is_contract_owned():
    result = compile_()
    ast = sqlglot.parse_one(result.operation.statement, read="postgres")
    assert isinstance(ast.find(exp.Count).this, exp.Star)
    assert result.operation.parameters == {}
    assert result.binding == MetricBinding(metric_id="orders_count")
    context = definition()
    context.metrics[0].aggregate.kind = "non_null_count"
    context.metrics[0].aggregate.field = "orders.id"
    result = compile_(context=context)
    ast = sqlglot.parse_one(result.operation.statement, read="postgres")
    assert isinstance(ast.find(exp.Count).this, exp.Column)
    assert not result.errors


def test_fixed_rule_and_dates_are_not_model_parameters():
    output = plan(
        "regional_revenue",
        [LLMParameter(name="region", type="string", value="부산")],
        LLMPeriod(kind="rolling_days", days=30),
    )
    result = compile_(output)
    operation = result.operation
    assert operation.parameters == {
        "region": "부산",
        "start_at": "2026-09-02T09:00:00+09:00",
        "end_at": "2026-10-02T09:00:00+09:00",
    }
    ast = sqlglot.parse_one(operation.statement, read="postgres")
    assert ast.find(exp.Sum).this.name == "amount"
    assert len(list(ast.find_all(exp.Join))) == 1
    assert [n.this for n in ast.find_all(exp.Literal)] == ["COMPLETED"]
    assert {n.name for n in ast.find_all(exp.Placeholder)} == {"region", "start_at", "end_at"}
    assert result.binding.time_field == "orders.paid_at"


@pytest.mark.parametrize(
    "params",
    [
        [],
        [LLMParameter(name="state", type="string", value="COMPLETED")],
        [LLMParameter(name="region", type="integer", value="1")],
        [LLMParameter(name="region", type="string", value="서울")] * 2,
    ],
)
def test_missing_extra_wrong_type_duplicate_inputs_rejected(params):
    assert compile_(plan("regional_revenue", params)).errors


def test_unknown_or_unexpected_metric_rejected():
    assert compile_(plan("unknown")).errors
    assert compile_(metric_id="regional_revenue").errors


def test_no_date_basis_asks_only_when_needed():
    output = plan(period=LLMPeriod(kind="previous_month", days=None))
    result = compile_(output)
    assert result.operation is None and result.clarifications and not result.errors
    assert compile_().operation is not None
    assert (
        compile_(output, question="orders.ordered_at last month").binding.time_field
        == "orders.ordered_at"
    )
    assert compile_(output, date_field="orders.paid_at").binding.time_field == "orders.paid_at"
    assert compile_(output, date_field="orders.id").errors
    assert compile_(output, question="paid_at and ordered_at last month").clarifications


def test_explicit_field_precedes_default_and_override_precedes_field():
    output = plan(
        "regional_revenue",
        [LLMParameter(name="region", type="string", value="서울")],
        LLMPeriod(kind="previous_month", days=None),
    )
    assert (
        compile_(output, question="ordered_at last month").binding.time_field == "orders.ordered_at"
    )
    assert (
        compile_(
            output, question="ordered_at last month", date_field="orders.paid_at"
        ).binding.time_field
        == "orders.paid_at"
    )


@pytest.mark.parametrize(
    "question,expected",
    [
        ("orders.ordered_at을 기준으로 최근 14일 주문 건수", "orders.ordered_at"),
        ("paid_at으로 계산한 최근 14일 주문 건수", "orders.paid_at"),
        ("other_orders.ordered_at recent orders", None),
        ("ordered_at_archive recent orders", None),
    ],
)
def test_explicit_fields_allow_korean_particles_but_not_other_identifiers(question, expected):
    result = compile_(plan(period=LLMPeriod(kind="rolling_days", days=14)), question=question)
    if expected:
        assert result.binding.time_field == expected
    else:
        assert result.clarifications and result.operation is None


def test_fixed_values_and_input_values_cannot_escape_sql():
    context = definition()
    context.metrics[1].fixed_filters[0].value = "COMPLETED'; DROP TABLE orders; --"
    result = compile_(
        plan(
            "regional_revenue",
            [LLMParameter(name="region", type="string", value="서울'; DROP TABLE orders; --")],
        ),
        context=context,
    )
    statements = sqlglot.parse(result.operation.statement, read="postgres")
    assert len(statements) == 1 and isinstance(statements[0], exp.Select)
    assert statements[0].find(exp.Literal).this == context.metrics[1].fixed_filters[0].value
    assert "DROP TABLE" not in result.operation.statement.replace(
        statements[0].find(exp.Literal).sql(dialect="postgres"), ""
    )
    assert result.operation.parameters["region"].endswith("; --")


def test_non_finite_model_input_is_rejected():
    context = definition()
    context.metrics[0].inputs = [MetricInput(name="minimum", type="number", field="orders.amount")]
    assert compile_(
        plan(parameters=[LLMParameter(name="minimum", type="number", value="1e999")]),
        context=context,
    ).errors


@pytest.mark.parametrize(
    "period",
    [
        LLMPeriod(kind="rolling_days", days=None),
        LLMPeriod(kind="rolling_days", days=0),
        LLMPeriod(kind="rolling_days", days=3661),
        LLMPeriod(kind="previous_day", days=1),
    ],
)
def test_invalid_period_fails_closed(period):
    assert compile_(plan(period=period), date_field="orders.paid_at").errors


@pytest.mark.parametrize(
    "kind,reference,zone,start,end",
    [
        ("previous_month", "2026-01-14T09:00:00+09:00", "Asia/Seoul", "2025-12-01", "2026-01-01"),
        ("previous_month", "2024-03-01T09:00:00+09:00", "Asia/Seoul", "2024-02-01", "2024-03-01"),
        ("previous_week", "2026-10-07T09:00:00+09:00", "Asia/Seoul", "2026-09-28", "2026-10-05"),
        ("previous_day", "2024-03-01T09:00:00+09:00", "Asia/Seoul", "2024-02-29", "2024-03-01"),
    ],
)
def test_calendar_bounds(kind, reference, zone, start, end):
    assert period_bounds(Period(kind=kind), datetime.fromisoformat(reference), zone, "date") == (
        start,
        end,
    )


def test_dst_is_local_calendar_day_not_24_hours():
    start, end = period_bounds(
        Period(kind="previous_day"),
        datetime.fromisoformat("2026-03-09T12:00:00-04:00"),
        "America/New_York",
        "date-time",
    )
    assert start == "2026-03-08T00:00:00-05:00"
    assert end == "2026-03-09T00:00:00-04:00"
    assert (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds() == 23 * 3600


def test_period_override_and_date_boundaries():
    context = definition()
    context.resources[0].fields[4].type = "date"
    result = compile_(
        context=context,
        date_field="orders.paid_at",
        period_override=Period(kind="rolling_days", days=30),
    )
    assert result.operation.parameters == {"start_at": "2026-09-02", "end_at": "2026-10-02"}
    assert result.operation.parameter_types == {"start_at": "date", "end_at": "date"}


@pytest.mark.parametrize(
    "patch",
    [
        {"resource": "missing"},
        {"aggregate": {"kind": "row_count", "field": "orders.id"}},
        {"aggregate": {"kind": "sum", "field": "orders.status"}},
        {"aggregate": {"kind": "non_null_count"}},
        {"aggregate": {"kind": "sum", "field": "orders.amount", "distinct": True}},
        {"group_by": ["orders.missing"]},
        {"group_by": ["orders.id", "ORDERS.ID"]},
        {"time_fields": ["orders.id"]},
        {"default_time_field": "orders.id"},
        {"inputs": [{"name": "start_at", "type": "string", "field": "orders.status"}]},
        {"fixed_filters": [{"field": "orders.id", "value": "not a number"}]},
        {"fixed_filters": [{"field": "orders.amount", "value": float("inf")}]},
        {"joins": [{"from": "orders.id", "to": "customers.id"}]},
        {
            "fixed_filters": [{"field": "orders.status", "value": "COMPLETED"}],
            "inputs": [{"name": "status", "type": "string", "field": "orders.status"}],
        },
    ],
)
def test_invalid_contracts(patch):
    raw = copy.deepcopy(DEFINITION)
    raw["metrics"][0].update(patch)
    assert definition_issues(ContextDefinition.model_validate(raw))


def test_contract_sql_is_immutable_except_formatting_and_comments():
    context = definition()
    result = compile_(context=context)
    op, binding = result.operation, result.binding
    assert not binding_issues(context, binding, "/* reviewed */ " + op.statement + ";", {})
    assert binding_issues(
        context, binding, op.statement.replace("COUNT(*)", 'COUNT("orders"."id")'), {}
    )
    assert binding_issues(context, binding, op.statement, {"status": "string"})
    context.metrics.clear()
    assert binding_issues(context, binding, op.statement, {})


def test_tampered_fixed_filter_and_date_are_rejected():
    result = compile_(
        plan(
            "regional_revenue",
            [LLMParameter(name="region", type="string", value="서울")],
            LLMPeriod(kind="previous_month", days=None),
        )
    )
    op, binding = result.operation, result.binding
    for statement in (
        op.statement.replace("'COMPLETED'", ":status"),
        op.statement.replace("paid_at", "ordered_at"),
        op.statement.replace("SUM", "AVG"),
    ):
        assert statement != op.statement, "the tampering must change the SQL"
        assert binding_issues(definition(), binding, statement, op.parameter_types)


def test_ddl_preserves_contracts_and_warns_on_removed_fields():
    result = import_ddl(definition(), "CREATE TABLE orders (id bigint);", "postgresql", "replace")
    assert result.definition.metrics == definition().metrics
    assert any("Metric" in w for w in result.warnings)


def test_planner_schema_has_no_sql_and_portable_required_fields():
    schema = LLMMetricPlan.model_json_schema()
    assert "statement" not in schema["properties"]
    for node in [schema, *schema["$defs"].values()]:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
    with pytest.raises(ValidationError):
        Period(kind="rolling_days", days=True)
    with pytest.raises(ValidationError):
        LLMPeriod(kind="rolling_days", days=True)


@pytest.mark.parametrize("kind", [" UUID ", "boolean", "timestamp[]", "jsonb"])
def test_min_max_do_not_accept_unsupported_field_types(kind):
    context = definition()
    context.resources[0].fields[0].type = kind
    context.metrics[0].aggregate.kind = "min"
    context.metrics[0].aggregate.field = "orders.id"
    assert definition_issues(context)
