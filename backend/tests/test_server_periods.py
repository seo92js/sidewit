"""Relative periods are computed by the server for model-written SQL and saved Tool calls."""

from datetime import datetime

import pytest
import sqlglot

from sidewit.generation.dto import LLMParameter, LLMPeriodBounds
from sidewit.generation.periods import bind_periods
from sidewit.generation.tool_plan import ToolPlanSelection
from sidewit.validation.periods import period_inputs, period_usage_issues
from tests.test_generate_api import ask, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_hybrid_api import hybrid
from tests.test_hybrid_api import hybrid_context as hybrid_context
from tests.test_metric_api import ask as ask_metric
from tests.test_metric_api import install

REFERENCE = datetime.fromisoformat("2026-10-02T09:00:00+09:00")  # a Friday
LAST_WEEK = {"start_at": "2026-09-21T00:00:00+09:00", "end_at": "2026-09-28T00:00:00+09:00"}
COUNT_SQL = "SELECT COUNT(*) FROM orders WHERE ordered_at >= :start_at AND ordered_at < :end_at"
WRONG_DATES = [
    LLMParameter(name="start_at", type="date-time", value="2026-09-20T00:00:00+09:00"),
    LLMParameter(name="end_at", type="date-time", value="2026-09-27T00:00:00+09:00"),
]


def week(start="start_at", end="end_at", kind="previous_week", days=None):
    return LLMPeriodBounds(kind=kind, days=days, start=start, end=end)


def usage(where):
    ast = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {where}", read="postgres")
    return [i.ref for i in period_usage_issues(ast, [("s", "e")])]


@pytest.mark.parametrize(
    "where",
    [
        "x >= :s AND x < :e",
        ":s <= x AND :e > x",
        "(x >= (:s)) AND x < :e",
        "x BETWEEN :s AND y AND x < :e",
        "NOT (x < :s) AND x < :e",
    ],
)
def test_half_open_bounds_pass(where):
    assert usage(where) == []


@pytest.mark.parametrize(
    ("where", "refs"),
    [
        ("x >= :s AND x <= :e", ["e"]),
        ("x BETWEEN :s AND :e", ["e"]),
        ("x > :s AND x < :e", ["s"]),
        ("x >= :s::date AND x < :e", ["s"]),
        ("x >= :e AND x < :s", ["e", "s"]),
        ("(x, y) >= (:s, 1) AND x < :e", ["s"]),
        ("x >= :s AND x < :e AND y = :s", ["s"]),
        (":s >= :e", ["e", "s"]),
        ("NOT (x >= :s) AND x < :e", ["s"]),
        ("NOT (x >= :s OR y = 1) AND x < :e", ["s"]),
    ],
)
def test_other_uses_are_rejected(where, refs):
    assert usage(where) == refs


def test_bind_periods_computes_values_and_rejects_bad_declarations():
    types = {"start_at": "date-time", "end_at": "date-time", "d1": "date", "d2": "date"}
    bound = bind_periods(
        [week(), week("d1", "d2", "rolling_days", 7)], types, REFERENCE, "Asia/Seoul"
    )
    assert bound.errors == []
    assert bound.values == LAST_WEEK | {"d1": "2026-09-25", "d2": "2026-10-02"}
    assert bound.parameters == (("start_at", "end_at"), ("d1", "d2"))

    for periods in (
        [week(end="start_at")],
        [week(), week("start_at", "d2")],
        [week(end="missing")],
        [week(end="d2")],
        [week(days=7)],
        [week(kind="rolling_days")],
    ):
        bound = bind_periods(periods, types, REFERENCE, "Asia/Seoul")
        assert [e.code for e in bound.errors] == ["period_invalid"]


async def test_server_replaces_model_written_dates(client, context, fake):
    output = ok(COUNT_SQL, WRONG_DATES).model_copy(update={"periods": [week()]})
    provider = fake(output)
    res = await ask(client, context, question="지난주 주문 건수")
    step = res.json()["operations"][0]
    assert step["operation"]["parameters"] == LAST_WEEK
    assert "periods" in step["validation"]["checks"]
    assert len(provider.calls) == 1


async def test_inclusive_end_is_repaired(client, context, fake):
    inclusive = COUNT_SQL.replace("< :end_at", "<= :end_at")
    provider = fake(
        ok(inclusive, WRONG_DATES).model_copy(update={"periods": [week()]}),
        ok(COUNT_SQL, WRONG_DATES).model_copy(update={"periods": [week()]}),
    )
    res = await ask(client, context, question="지난주 주문 건수")
    assert "[period_bound_misused]" in provider.calls[1][1][-1].content
    assert res.json()["operations"][0]["operation"]["parameters"] == LAST_WEEK


async def test_undeclared_period_parameter_is_repaired(client, context, fake):
    provider = fake(
        ok(COUNT_SQL, WRONG_DATES[:1]).model_copy(update={"periods": [week()]}),
        ok(COUNT_SQL, WRONG_DATES).model_copy(update={"periods": [week()]}),
    )
    res = await ask(client, context, question="지난주 주문 건수")
    assert "[period_invalid]" in provider.calls[1][1][-1].content
    assert res.json()["operations"][0]["operation"]["parameters"] == LAST_WEEK


@pytest.mark.parametrize(
    ("first", "message"),
    [
        (
            hybrid("metric", metric_id="regional_revenue").model_copy(update={"periods": [week()]}),
            "`periods` must be empty",
        ),
        (
            hybrid("sql", statement=COUNT_SQL, parameters=WRONG_DATES, date_field="orders.paid_at"),
            "`period` and `date_field` must be null",
        ),
    ],
)
async def test_hybrid_rejects_a_period_in_the_other_routes_field(
    client, hybrid_context, first, message
):
    second = hybrid("sql", statement=COUNT_SQL, parameters=WRONG_DATES).model_copy(
        update={"periods": [week()]}
    )
    provider = install(first, second)
    res = await ask_metric(client, hybrid_context, question="지난주 주문 건수")
    assert message in provider.calls[1][1][-1].content
    assert res.json()["operations"][0]["operation"]["parameters"] == LAST_WEEK


async def saved_range_tool(client, context, statement):
    response = await client.post(
        "/api/v1/tools",
        json={
            "context_id": context["id"],
            "name": "orders_between",
            "description": "Order count in a range.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_at": {"type": "string", "format": "date-time"},
                    "end_at": {"type": "string", "format": "date-time"},
                },
                "required": ["start_at", "end_at"],
                "additionalProperties": False,
            },
            "operation": {"type": "sql", "statement": statement},
        },
    )
    assert response.status_code == 201, response.text


def tool_plan(periods):
    return ToolPlanSelection(
        route="plan",
        calls=[{"tool_name": "orders_between", "parameters": [], "periods": periods}],
        generated_tasks=[],
        reason=None,
        clarifications=[],
    )


async def test_tool_call_period_is_filled_by_the_server(client, context, fake):
    await saved_range_tool(client, context, COUNT_SQL)
    fake(tool_plan([week()]))
    res = await ask(client, context, question="지난주 주문 건수")
    assert res.status_code == 200, res.text
    assert res.json()["operations"][0]["operation"]["parameters"] == LAST_WEEK


async def test_tool_with_inclusive_end_rejects_a_period(client, context, fake):
    await saved_range_tool(client, context, COUNT_SQL.replace("< :end_at", "<= :end_at"))
    fake(tool_plan([week()]))
    res = await ask(client, context, question="지난주 주문 건수")
    assert res.status_code == 422
    assert "period_bound_misused" in res.text


@pytest.mark.parametrize(
    ("where", "pairs"),
    [
        ("x >= :s AND x < :e", [("s", "e")]),
        (":e > x AND (x >= :s)", [("s", "e")]),
        ("x BETWEEN :s AND :e", []),
        ("x >= :s::date AND x < :e", []),
        ("x >= :s AND y < :e", []),
        ("a >= :s1 AND a < :e1 AND b >= :s2 AND b < :e2", [("s1", "e1"), ("s2", "e2")]),
    ],
)
def test_period_inputs_are_half_open_pairs_on_the_same_expression(where, pairs):
    ast = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {where}", read="postgres")
    assert period_inputs(ast, ["s", "e", "s1", "e1", "s2", "e2"]) == pairs


async def test_catalog_lists_period_inputs_only_for_half_open_tools(client, context, fake):
    await saved_range_tool(client, context, COUNT_SQL)
    provider = fake(tool_plan([week()]))
    await ask(client, context, question="지난주 주문 건수")
    assert '"period_inputs": [{"start": "start_at", "end": "end_at"}]' in provider.calls[0][0]


async def test_catalog_has_no_period_inputs_for_an_inclusive_tool(client, context, fake):
    await saved_range_tool(client, context, COUNT_SQL.replace("< :end_at", "<= :end_at"))
    provider = fake(tool_plan([week()]))
    await ask(client, context, question="지난주 주문 건수")
    assert '"period_inputs": []' in provider.calls[0][0]
