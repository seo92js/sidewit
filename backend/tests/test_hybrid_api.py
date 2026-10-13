"""Hybrid Generate: registered metrics first, model-written SQL when no metric fits."""

import pytest

from sidewit.generation.dto import LLMParameter, LLMToolSuggestion
from sidewit.generation.metric_plan import LLMHybridPlan, LLMPeriod
from tests.test_metric_api import ask, install
from tests.test_metric_contracts import DEFINITION, plan

REVENUE_SQL = (
    "SELECT SUM(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.region = :region AND o.amount >= :minimum AND o.status = 'COMPLETED'"
)


def hybrid(route, *, metric_id=None, period=None, date_field=None, statement=None, parameters=()):
    return LLMHybridPlan(
        status="ok",
        reason=None,
        clarifications=[],
        route=route,
        metric_id=metric_id,
        date_field=date_field,
        period=period,
        statement=statement,
        parameters=list(parameters),
        tool_suggestion=LLMToolSuggestion(name="answer", description="Answer"),
    )


def metric_route():
    return hybrid(
        "metric",
        metric_id="regional_revenue",
        date_field="orders.paid_at",
        period=LLMPeriod(kind="rolling_days", days=30),
        parameters=[LLMParameter(name="region", type="string", value="서울")],
    )


def sql_route(statement=REVENUE_SQL):
    return hybrid(
        "sql",
        statement=statement,
        parameters=[
            LLMParameter(name="region", type="string", value="서울"),
            LLMParameter(name="minimum", type="number", value="3000"),
        ],
    )


@pytest.fixture
async def hybrid_context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "hybrid",
            "dialect": "postgresql",
            "timezone": "Asia/Seoul",
            "definition": DEFINITION,
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["allow_generated_sql"] is True
    return res.json()


async def detail(client, generation_id):
    return (await client.get(f"/api/v1/generations/{generation_id}")).json()


async def test_metric_route_is_contract_built(client, hybrid_context):
    provider = install(metric_route())
    res = await ask(client, hybrid_context, question="최근 30일 서울 매출")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "metric"
    assert body["metric_binding"] == {
        "metric_id": "regional_revenue",
        "time_field": "orders.paid_at",
    }
    assert "'COMPLETED'" in body["operation"]["statement"]
    assert body["operation"]["parameters"]["region"] == "서울"
    assert "metric_contract" in body["validation"]["checks"]
    record = await detail(client, body["generation_id"])
    assert (record["prompt_version"], record["source"]) == ("hybrid_v4", "metric")

    system = provider.calls[0][0]
    assert "# SQL rules for `ok`" in system, "the SQL route rules are included"
    assert "# Registered metrics — check these first" in system
    assert "- regional_revenue: Paid revenue in a region" in system
    assert "fixed filters: orders.status = 'COMPLETED'" in system


async def test_sql_route_when_no_metric_fits(client, hybrid_context):
    install(sql_route())
    res = await ask(client, hybrid_context, question="서울 지역 3000원 이상 매출")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "generated_sql"
    assert body["metric_binding"] is None
    assert body["operation"]["statement"] == REVENUE_SQL
    assert "request_literals" in body["validation"]["checks"]


async def test_sql_route_still_requires_request_values_as_parameters(client, hybrid_context):
    literal = hybrid(
        "sql",
        statement=REVENUE_SQL.replace(":region", "'서울'"),
        parameters=[LLMParameter(name="minimum", type="number", value="3000")],
    )
    provider = install(literal, sql_route())
    res = await ask(client, hybrid_context, question="서울 지역 3000원 이상 매출")
    assert res.status_code == 200
    assert "request_value_literal" in provider.calls[1][1][2].content


async def test_ok_without_route_is_repaired(client, hybrid_context):
    provider = install(hybrid(None), metric_route())
    res = await ask(client, hybrid_context, question="최근 30일 서울 매출")
    assert res.status_code == 200
    assert res.json()["source"] == "metric"
    assert "route_missing" in provider.calls[1][1][2].content


async def test_repair_may_switch_route(client, hybrid_context):
    broken_sql = hybrid("sql", statement="SELECT SUM(nope) FROM orders")
    provider = install(broken_sql, metric_route())
    res = await ask(client, hybrid_context, question="최근 30일 서울 매출")
    assert res.status_code == 200
    assert res.json()["source"] == "metric"
    repair = provider.calls[1][1][2].content
    assert "unknown_field" in repair and "You may switch the route" in repair
    assert (await detail(client, res.json()["generation_id"]))["attempts"] == 2


async def test_invalid_metric_choice_is_repaired(client, hybrid_context):
    unknown = hybrid("metric", metric_id="no_such_metric")
    install(unknown, metric_route())
    res = await ask(client, hybrid_context, question="최근 30일 서울 매출")
    assert res.status_code == 200 and res.json()["source"] == "metric"


@pytest.mark.parametrize("status", ["ambiguous", "unanswerable"])
async def test_non_ok_answers_have_no_route_or_source(client, hybrid_context, status):
    install(
        LLMHybridPlan(
            status=status,
            reason="r",
            clarifications=[{"question": "q?", "options": []}] if status == "ambiguous" else [],
            route="sql",
            metric_id=None,
            date_field=None,
            period=None,
            statement="SELECT 1",
            parameters=[],
            tool_suggestion=None,
        )
    )
    res = await ask(client, hybrid_context, question="질문")
    assert res.json()["status"] == status
    assert "source" not in res.json() and "SELECT" not in res.text
    assert (await detail(client, res.json()["generation_id"]))["source"] is None


async def test_require_metric_uses_the_metric_planner(client, hybrid_context):
    install(plan("orders_count"))
    res = await ask(client, hybrid_context, require_metric=True)
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "metric"
    record = await detail(client, res.json()["generation_id"])
    assert record["prompt_version"] == "metric_plan_v3"


async def test_metrics_only_context_never_routes_to_sql(client, hybrid_context):
    from tests.test_metric_api import put_context

    assert (await put_context(client, hybrid_context, allow_generated_sql=False)).status_code == 200
    install(plan("orders_count"))
    res = await ask(client, hybrid_context)
    assert (await detail(client, res.json()["generation_id"]))["prompt_version"] == (
        "metric_plan_v3"
    )


async def test_context_without_metrics_uses_the_sql_planner(client):
    from tests.test_generate_api import ok

    context = (
        await client.post("/api/v1/contexts", json={"name": "plain", "dialect": "postgresql"})
    ).json()
    install(ok("SELECT 1", []))
    res = await ask(client, context)
    assert res.json()["source"] == "generated_sql"
    assert (await detail(client, res.json()["generation_id"]))["prompt_version"] == "generate_v9"


async def test_tool_drafts_keep_the_route(client, hybrid_context):
    install(metric_route(), sql_route())
    for expected_binding in (True, False):
        generation = (await ask(client, hybrid_context, question="서울 매출")).json()
        draft = (
            await client.post(
                "/api/v1/tools/draft", json={"generation_id": generation["generation_id"]}
            )
        ).json()
        assert (draft["metric_binding"] is not None) is expected_binding
