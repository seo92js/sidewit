import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sidewit.api.generate import get_llm_provider
from sidewit.generation.dto import LLMGenerationOutput, LLMParameter, LLMToolSuggestion
from sidewit.generation.providers.fake import FakeProvider
from sidewit.generation.providers.gemini import GeminiProvider
from sidewit.main import app

DEFINITION = {
    "resources": [
        {
            "name": "customers",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "region", "type": "text", "description": "고객 지역"},
            ],
        },
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "customer_id", "type": "bigint"},
                {"name": "amount", "type": "numeric", "description": "실제 결제 금액"},
                {"name": "status", "type": "text", "allowed_values": ["COMPLETED", "CANCELLED"]},
                {"name": "ordered_at", "type": "timestamptz", "description": "주문 생성 시각"},
                {"name": "paid_at", "type": "timestamptz", "description": "결제 완료 시각"},
            ],
        },
    ],
    "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
    "rules": [{"id": "rule_1", "text": "매출은 status = 'COMPLETED'인 주문만 포함한다."}],
}

REVENUE_SQL = (
    "SELECT SUM(o.amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.region = :region AND o.paid_at >= :start_at AND o.paid_at < :end_at "
    "AND o.status = 'COMPLETED'"
)
REVENUE_PARAMS = [
    LLMParameter(name="region", type="string", value="서울"),
    LLMParameter(name="start_at", type="date-time", value="2026-09-02T00:00:00+09:00"),
    LLMParameter(name="end_at", type="date-time", value="2026-10-02T00:00:00+09:00"),
]
REFERENCE_TIME = "2026-10-02T09:00:00+09:00"


def ok(statement=REVENUE_SQL, parameters=REVENUE_PARAMS, suggestion=True):
    return LLMGenerationOutput(
        status="ok",
        reason=None,
        clarifications=[],
        statement=statement,
        parameters=parameters,
        tool_suggestion=LLMToolSuggestion(name="regional_revenue", description="지역 매출")
        if suggestion
        else None,
    )


def not_ok(status, reason, clarifications=(), statement=None):
    return LLMGenerationOutput(
        status=status,
        reason=reason,
        clarifications=list(clarifications),
        statement=statement,
        parameters=[],
        tool_suggestion=None,
    )


@pytest.fixture
def fake():
    def install(*responses):
        provider = FakeProvider(list(responses))
        app.dependency_overrides[get_llm_provider] = lambda: provider
        return provider

    return install


@pytest.fixture
async def context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "commerce",
            "dialect": "postgresql",
            "timezone": "Asia/Seoul",
            "definition": DEFINITION,
        },
    )
    assert res.status_code == 201
    return res.json()


async def ask(client, context, question="최근 30일 서울 결제 매출", **extra):
    body = {"context_id": context["id"], "question": question, "reference_time": REFERENCE_TIME}
    return await client.post("/api/v1/generate", json=body | extra)


async def detail(client, generation_id):
    res = await client.get(f"/api/v1/generations/{generation_id}")
    assert res.status_code == 200
    return res.json()


# ok -------------------------------------------------------------------------------------


async def test_ok_returns_validated_parameterized_operation(client, context, fake):
    provider = fake(ok())
    res = await ask(client, context)
    assert res.status_code == 200
    body = res.json()

    assert set(body) == {
        "generation_id",
        "context_version",
        "status",
        "operation",
        "validation",
        "tool_suggestion",
        "metric_binding",
        "source",
    }
    assert body["source"] == "generated_sql"
    assert body["metric_binding"] is None
    assert body["status"] == "ok"
    assert body["context_version"] == 1
    assert body["operation"] == {
        "type": "sql",
        "dialect": "postgresql",
        "statement": REVENUE_SQL,
        "parameters": {
            "region": "서울",
            "start_at": "2026-09-02T00:00:00+09:00",
            "end_at": "2026-10-02T00:00:00+09:00",
        },
        "parameter_types": {"region": "string", "start_at": "date-time", "end_at": "date-time"},
    }
    assert body["validation"]["valid"] is True
    assert "relative_time" in body["validation"]["checks"]
    assert body["tool_suggestion"] == {"name": "regional_revenue", "description": "지역 매출"}
    assert len(provider.calls) == 1

    record = await detail(client, body["generation_id"])
    assert record["status"] == "ok"
    assert record["attempts"] == 1
    assert record["provider"] == "fake"
    assert record["prompt_version"] == "generate_v8"
    assert record["reference_time"] == "2026-10-02T00:00:00Z"


async def test_parameter_values_are_converted_to_declared_types(client, context, fake):
    fake(
        ok(
            "SELECT id FROM orders WHERE amount > :min_amount LIMIT :n",
            [
                LLMParameter(name="min_amount", type="number", value="10.5"),
                LLMParameter(name="n", type="integer", value="30"),
            ],
        )
    )
    body = (await ask(client, context)).json()
    assert body["operation"]["parameters"] == {"min_amount": 10.5, "n": 30}


async def test_non_finite_number_rejected_without_json_storage_error(client, context, fake):
    response = ok(
        "SELECT id FROM orders WHERE amount > :minimum",
        [LLMParameter(name="minimum", type="number", value="1e999")],
    )
    fake(response, response)
    res = await ask(client, context, question="orders over minimum amount")
    assert res.status_code == 422
    record = await detail(client, res.json()["details"]["generation_id"])
    assert record["operation"]["parameters"] == {"minimum": "1e999"}
    assert record["status"] == "failed"


async def test_question_and_reference_time_reach_the_provider(client, context, fake):
    provider = fake(ok())
    await ask(client, context, question="  최근 30일 서울 결제 매출  ")
    system, messages = provider.calls[0]
    assert [(m.role, m.content) for m in messages] == [("user", "최근 30일 서울 결제 매출")]
    assert "2026-10-02T09:00:00+09:00 (Friday), timezone Asia/Seoul" in system
    assert "paid_at (timestamptz): 결제 완료 시각" in system
    assert "[rule_1] 매출은 status = 'COMPLETED'인 주문만 포함한다." in system


async def test_reference_time_defaults_to_now(client, context, fake):
    fake(ok())
    body = {"context_id": context["id"], "question": "매출"}
    res = await client.post("/api/v1/generate", json=body)
    assert res.status_code == 200
    record = await detail(client, res.json()["generation_id"])
    assert record["reference_time"] is not None


async def test_naive_reference_time_is_rejected(client, context, fake):
    provider = fake(ok())
    res = await ask(client, context, reference_time="2026-10-02T09:00:00")
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"
    assert provider.calls == []


# ambiguous / unanswerable ---------------------------------------------------------------


async def test_ambiguous_returns_clarifications_without_sql(client, context, fake):
    provider = fake(
        not_ok(
            "ambiguous",
            "Several date fields could define the period.",
            ["Should the period be based on ordered_at or paid_at?"],
            statement="SELECT id FROM orders",
        ),
        ok(),
    )
    res = await ask(client, context, question="최근 30일 거래")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "generation_id": body["generation_id"],
        "context_version": 1,
        "status": "ambiguous",
        "reason": "Several date fields could define the period.",
        "clarifications": ["Should the period be based on ordered_at or paid_at?"],
    }
    assert "SELECT" not in res.text
    assert len(provider.calls) == 1, "no repair or second call for ambiguous"
    record = await detail(client, body["generation_id"])
    assert record["operation"] is None
    assert record["validation"] is None


async def test_unanswerable_returns_reason_without_sql(client, context, fake):
    provider = fake(
        not_ok("unanswerable", "No churn data in the context.", statement="SELECT 1"),
        ok(),
    )
    res = await ask(client, context, question="이탈률 알려줘")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "generation_id": body["generation_id"],
        "context_version": 1,
        "status": "unanswerable",
        "reason": "No churn data in the context.",
    }
    assert "SELECT" not in res.text
    assert len(provider.calls) == 1


# repair ---------------------------------------------------------------------------------


async def test_repair_once_then_succeed(client, context, fake):
    bad = ok("SELECT SUM(amt) FROM orders WHERE status = 'COMPLETED'", [], suggestion=False)
    provider = fake(bad, ok())
    res = await ask(client, context)
    assert res.status_code == 200
    assert res.json()["operation"]["statement"] == REVENUE_SQL

    assert len(provider.calls) == 2
    _, repair_messages = provider.calls[1]
    assert [m.role for m in repair_messages] == ["user", "assistant", "user"]
    assert json.loads(repair_messages[1].content)["statement"] == bad.statement
    assert "[unknown_field]" in repair_messages[2].content
    assert "amt" in repair_messages[2].content

    record = await detail(client, res.json()["generation_id"])
    assert record["attempts"] == 2
    assert record["status"] == "ok"


async def test_user_literal_triggers_repair_and_keeps_rule_constant(client, context, fake):
    bad = ok(REVENUE_SQL.replace(":region", "'서울'"), REVENUE_PARAMS[1:])
    provider = fake(bad, ok())
    response = await ask(client, context)
    assert response.status_code == 200
    assert response.json()["operation"]["statement"] == REVENUE_SQL
    assert "request_literals" in response.json()["validation"]["checks"]
    assert "[request_value_literal]" in provider.calls[1][1][-1].content
    assert (await detail(client, response.json()["generation_id"]))["attempts"] == 2


async def test_repeated_user_literal_is_rejected_without_public_sql_or_tool_draft(
    client, context, fake
):
    bad = ok(REVENUE_SQL.replace(":region", "'서울'"), REVENUE_PARAMS[1:])
    provider = fake(bad, bad)
    response = await ask(client, context)
    assert response.status_code == 422 and len(provider.calls) == 2
    body = response.json()
    assert body["error"] == "generation_invalid"
    assert body["details"]["validation_errors"][0]["code"] == "request_value_literal"
    assert "SELECT" not in response.text and "서울" not in response.text
    draft = await client.post(
        "/api/v1/tools/draft", json={"generation_id": body["details"]["generation_id"]}
    )
    assert draft.status_code == 409


async def test_rule_constant_mentioned_in_question_is_preserved(client, context, fake):
    provider = fake(ok("SELECT SUM(amount) FROM orders WHERE status = 'COMPLETED'", []))
    response = await ask(client, context, question="COMPLETED 매출 합계")
    assert response.status_code == 200 and len(provider.calls) == 1
    assert response.json()["operation"]["parameters"] == {}


async def test_request_parameter_repair_can_end_with_clarification(client, context, fake):
    bad = ok(REVENUE_SQL.replace(":region", "'서울'"), REVENUE_PARAMS[1:])
    provider = fake(bad, not_ok("ambiguous", "Period unclear.", ["Which period?"]))
    response = await ask(client, context)
    assert response.status_code == 200 and response.json()["status"] == "ambiguous"
    assert "operation" not in response.json() and len(provider.calls) == 2


async def test_relative_time_function_triggers_repair(client, context, fake):
    now_sql = (
        "SELECT SUM(amount) FROM orders WHERE paid_at >= NOW() - INTERVAL '30 days' "
        "AND status = 'COMPLETED'"
    )
    provider = fake(ok(now_sql, []), ok())
    res = await ask(client, context)
    assert res.status_code == 200
    assert "[relative_time_function]" in provider.calls[1][1][2].content


async def test_unconvertible_value_triggers_repair(client, context, fake):
    bad = ok(
        "SELECT id FROM orders LIMIT :n", [LLMParameter(name="n", type="integer", value="ten")]
    )
    provider = fake(bad, ok())
    res = await ask(client, context)
    assert res.status_code == 200
    assert "[parameter_type_mismatch]" in provider.calls[1][1][2].content


async def test_duplicate_parameters_trigger_repair(client, context, fake):
    bad = ok(
        "SELECT id FROM customers WHERE region = :region",
        [
            LLMParameter(name="region", type="string", value="서울"),
            LLMParameter(name="region", type="string", value="부산"),
        ],
    )
    provider = fake(bad, ok())
    res = await ask(client, context)
    assert res.status_code == 200
    assert "[duplicate_parameter]" in provider.calls[1][1][2].content


async def test_ok_without_statement_triggers_repair(client, context, fake):
    provider = fake(ok(statement=None, parameters=[]), ok())
    res = await ask(client, context)
    assert res.status_code == 200
    assert "[sql_parse_error]" in provider.calls[1][1][2].content


async def test_repair_may_conclude_unanswerable(client, context, fake):
    fake(ok("SELECT churn FROM orders", []), not_ok("unanswerable", "No churn data."))
    res = await ask(client, context)
    assert res.status_code == 200
    assert res.json()["status"] == "unanswerable"


async def test_repair_failure_is_422_without_sql(client, context, fake):
    first = "SELECT SUM(amt) FROM orders"
    second = "SELECT SUM(o.total) FROM orders o WHERE o.paid_at >= CURRENT_DATE"
    provider = fake(ok(first, []), ok(second, []), ok())
    res = await ask(client, context)

    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "generation_invalid"
    assert set(body["details"]) == {"generation_id", "validation_errors"}
    assert [e["code"] for e in body["details"]["validation_errors"]] == [
        "unknown_field",
        "relative_time_function",
    ]
    for leaked in (first, second, "SELECT", "statement", "operation"):
        assert leaked not in res.text
    assert len(provider.calls) == 2, "repair happens exactly once"

    record = await detail(client, body["details"]["generation_id"])
    assert record["status"] == "failed"
    assert record["attempts"] == 2
    assert record["operation"]["statement"] == second, "kept in the log for debugging"
    assert record["validation"]["valid"] is False


# provider failures ----------------------------------------------------------------------


async def test_llm_error_is_502_and_recorded(client, context, fake):
    fake(RuntimeError("upstream timeout"))
    res = await ask(client, context)
    assert res.status_code == 502
    body = res.json()
    assert body["error"] == "llm_error"
    assert body["message"] == "LLM call failed."
    assert "upstream timeout" not in res.text
    record = await detail(client, body["details"]["generation_id"])
    assert record["status"] == "failed"
    assert "upstream timeout" in record["reason"]


async def test_llm_error_during_repair_is_502(client, context, fake):
    fake(ok("SELECT amt FROM orders", []), RuntimeError("boom"))
    res = await ask(client, context)
    assert res.status_code == 502


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_gemini_transport_failure_is_502_and_recorded(client, context, error_type):
    key = "fixture-private-key"
    provider = GeminiProvider("gemini-test", key)
    request = AsyncMock(side_effect=error_type(f"connection failed: {key}"))
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=request))
    )
    app.dependency_overrides[get_llm_provider] = lambda: provider
    res = await ask(client, context)
    assert res.status_code == 502
    assert res.json()["error"] == "llm_error"
    record = await detail(client, res.json()["details"]["generation_id"])
    assert record["status"] == "failed"
    assert record["attempts"] == 1
    assert "Gemini request failed" in record["reason"]
    assert key not in json.dumps(record)
    assert key not in res.text
    request.assert_awaited_once()


async def test_not_configured_is_503_and_not_recorded(client, context):
    app.dependency_overrides[get_llm_provider] = lambda: None
    res = await ask(client, context)
    assert res.status_code == 503
    assert res.json()["error"] == "llm_not_configured"
    assert (await client.get("/api/v1/generations")).json()["items"] == []


async def test_unknown_context_is_404(client, fake):
    provider = fake(ok())
    res = await client.post(
        "/api/v1/generate",
        json={"context_id": "00000000-0000-0000-0000-000000000000", "question": "q"},
    )
    assert res.status_code == 404
    assert res.json()["error"] == "context_not_found"
    assert provider.calls == []


# provider DTO isolation -----------------------------------------------------------------


async def test_provider_dto_never_reaches_public_responses(client, context, fake):
    fake(ok(), not_ok("ambiguous", "r", ["q?"]), not_ok("unanswerable", "r"))
    dto_only_shapes = ('"value":', '"parameters":[', '"tool_suggestion":null', '"statement":null')
    for _ in range(3):
        res = await ask(client, context)
        assert res.status_code == 200
        compact = json.dumps(res.json(), separators=(",", ":"), ensure_ascii=False)
        for shape in dto_only_shapes:
            assert shape not in compact, (shape, compact)
        body = res.json()
        if body["status"] == "ok":
            assert isinstance(body["operation"]["parameters"], dict)
            assert "clarifications" not in body and "reason" not in body
        else:
            assert "operation" not in body and "tool_suggestion" not in body


# generation log -------------------------------------------------------------------------


async def test_list_feedback_and_cascade_delete(client, context, fake):
    fake(ok(), not_ok("unanswerable", "r"))
    first = (await ask(client, context)).json()["generation_id"]
    second = (await ask(client, context)).json()["generation_id"]

    items = (await client.get(f"/api/v1/generations?context_id={context['id']}")).json()["items"]
    assert [i["id"] for i in items] == [second, first]
    assert "operation" not in items[0], "summaries omit SQL"

    res = await client.post(f"/api/v1/generations/{first}/feedback", json={"feedback": "up"})
    assert res.status_code == 200
    assert res.json()["feedback"] == "up"
    res = await client.post(f"/api/v1/generations/{first}/feedback", json={"feedback": "meh"})
    assert res.status_code == 422

    await client.delete(f"/api/v1/contexts/{context['id']}")
    assert (await client.get(f"/api/v1/generations/{first}")).status_code == 404


async def test_generate_and_generations_require_api_key(client, context, fake, api_key):
    fake(ok())
    assert (await ask(client, context)).status_code == 401
    assert (await client.get("/api/v1/generations")).status_code == 401


async def test_credentials_in_provider_errors_are_not_persisted(client, context, fake):
    fake(RuntimeError("Incorrect API key provided: sk-proj-abcdef123456"))
    res = await ask(client, context)
    record = await detail(client, res.json()["details"]["generation_id"])
    assert "sk-proj-abcdef123456" not in record["reason"]
    assert "[REDACTED]" in record["reason"]


@pytest.mark.parametrize("hybrid_mode", [False, True])
@pytest.mark.parametrize("repair_succeeds", [False, True])
async def test_mixed_case_sql_gets_quoting_guidance_and_is_revalidated(
    client, fake, hybrid_mode, repair_succeeds
):
    from sidewit.generation.metric_plan import LLMHybridPlan

    definition = {
        "resources": [{"name": "Store", "fields": [{"name": "regionSido", "type": "text"}]}],
        "metrics": [
            {
                "id": "store_count",
                "description": "All stores",
                "resource": "Store",
                "aggregate": {"kind": "row_count"},
            }
        ]
        if hybrid_mode
        else [],
    }
    created = await client.post(
        "/api/v1/contexts",
        json={
            "name": "mixed_case",
            "dialect": "postgresql",
            "definition": definition,
        },
    )
    assert created.status_code == 201
    params = [LLMParameter(name="region_sido", type="string", value="서울")]
    bad_sql = "SELECT count(*) FROM Store WHERE regionSido = :region_sido"
    good_sql = 'SELECT count(*) FROM "Store" WHERE "regionSido" = :region_sido'

    def response(sql):
        output = ok(sql, params)
        if hybrid_mode:
            return LLMHybridPlan(**output.model_dump(), route="sql", metric_id=None, period=None)
        return output

    provider = fake(response(bad_sql), response(good_sql if repair_succeeds else bad_sql))
    res = await ask(client, created.json(), question="서울 매장 개수")
    assert len(provider.calls) == 2
    assert '"Store"."regionSido"' in provider.calls[0][0]
    repair = provider.calls[1][1][-1].content
    assert "unknown_resource" in repair
    assert '"Store"."regionSido"' in repair
    if repair_succeeds:
        assert res.status_code == 200
        assert res.json()["operation"]["statement"] == good_sql
        assert res.json()["operation"]["parameters"] == {"region_sido": "서울"}
    else:
        assert res.status_code == 422
        assert res.json()["error"] == "generation_invalid"
        assert "operation" not in res.json()
