"""Stateless continuations preserve inputs and use normal routing and validation."""

import json
import logging
from uuid import UUID

import pytest

from sidewit.generation.dto import LLMParameter
from sidewit.generation.metric_plan import LLMPeriod
from sidewit.generation.providers.base import StructuredResult
from tests.test_generate_api import REFERENCE_TIME, ask, not_ok, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_metric_api import metric_context as metric_context
from tests.test_metric_contracts import plan
from tests.test_tool_plan import call
from tests.test_tool_plan import plan as tool_plan
from tests.test_tool_selection import saved_tool


def ambiguous(question="Which region?", options=("서울", "부산")):
    return not_ok("ambiguous", "Choose a region.", [{"question": question, "options": options}])


def continuation(body, answer="서울"):
    q = body["clarification_questions"][0]
    return {
        "reference_time": body["reference_time"],
        "expected_context_version": body["context_version"],
        "clarification_answers": [{"id": q["id"], "question": q["question"], "answer": answer}],
    }


async def test_stateless_answer_survives_history_eviction_and_preserves_time(
    client, context, fake, history, caplog
):
    caplog.set_level(logging.INFO)
    provider = fake(
        ambiguous(),
        ok(
            "SELECT id FROM customers WHERE region = :region",
            [LLMParameter(name="region", type="string", value="서울")],
        ),
    )
    first = await ask(client, context, question="고객 목록", reference_time=None)
    body = first.json()
    assert [q["question"] for q in body["clarification_questions"]] == ["Which region?"]
    assert "clarifications" not in body
    assert body["clarification_questions"][0]["options"] == ["서울", "부산"]
    history.forget_context(UUID(context["id"]))
    payload = continuation(body)
    second = await ask(client, context, question="고객 목록", **payload)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    sent = json.loads(provider.calls[1][1][0].content)
    assert sent == {
        "question": "고객 목록",
        "clarification_answers": payload["clarification_answers"],
    }
    record = (await client.get(f"/api/v1/generations/{second.json()['generation_id']}")).json()
    assert record["reference_time"] == body["reference_time"]
    assert record["question"] == "고객 목록"
    assert record["clarification_answers"] == payload["clarification_answers"]
    assert "서울" not in caplog.text
    assert "generation id=" in caplog.text


async def test_answers_reach_tool_selection_and_fallback(client, context, fake):
    await saved_tool(client, context)
    provider = fake(
        tool_plan(
            [], "ambiguous", "Missing region", [{"question": "Which region?", "options": []}]
        ),
        tool_plan([call()]),
        tool_plan([], "generate"),
        ok("SELECT id FROM customers", []),
    )
    first = (await ask(client, context, question="고객 수")).json()
    payload = continuation(first)
    reused = await ask(client, context, question="고객 수", **payload)
    assert reused.json()["source"] == "plan"
    assert reused.json()["operations"][0]["operation"]["parameters"] == {"region": "서울"}
    payload["clarification_answers"][0]["answer"] = "지역 조건 없이 고객 id 목록으로 변경"
    generated = await ask(client, context, question="고객 수", **payload)
    assert generated.json()["source"] == "generated_sql"
    assert provider.calls[2][1][0] == provider.calls[3][1][0]


async def test_answer_literals_are_validated(client, context, fake):
    fake(
        ok("SELECT id FROM customers WHERE region = '서울'", []),
        ok("SELECT id FROM customers WHERE region = '서울'", []),
    )
    result = await ask(
        client,
        context,
        question="고객 목록",
        expected_context_version=1,
        clarification_answers=[{"id": "q1", "question": "서울 or 부산?", "answer": "서울"}],
    )
    assert result.status_code == 422
    assert result.json()["error"] == "generation_invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"reference_time": None},
        {"expected_context_version": None},
        {"clarification_answers": [{"id": "q", "question": "Region?", "answer": "  "}]},
        {"clarification_answers": [{"id": "q", "question": "Region?", "answer": "서울"}] * 2},
        {
            "clarification_answers": [
                {"id": str(i), "question": "Region?", "answer": "서울"} for i in range(25)
            ]
        },
        {
            "clarification_answers": [
                {"id": str(i), "question": "Region?", "answer": "a" * 2000} for i in range(8)
            ]
        },
    ],
)
async def test_invalid_continuation_before_provider(client, context, fake, change):
    provider = fake()
    payload = {
        "expected_context_version": 1,
        "reference_time": REFERENCE_TIME,
        "clarification_answers": [{"id": "q", "question": "Region?", "answer": "서울"}],
    }
    result = await ask(client, context, **(payload | change))
    assert result.status_code == 422, result.text
    assert not provider.calls


async def test_stale_context_before_provider(client, context, fake):
    provider = fake()
    result = await ask(client, context, expected_context_version=2)
    assert result.status_code == 409
    assert result.json()["error"] == "context_changed"
    assert not provider.calls


async def test_context_change_during_generation(client, context, fake, store):
    provider = fake()

    async def changed(*args):
        updated = await client.put(
            f"/api/v1/contexts/{context['id']}",
            json={**context, "name": "updated-context", "expected_version": context["version"]},
        )
        assert updated.status_code == 200
        return StructuredResult(parsed=ok("SELECT id FROM customers", []))

    provider.generate_structured = changed
    result = await ask(client, context, expected_context_version=1)
    assert result.status_code == 409
    assert "operation" not in result.json()
    record = (
        await client.get(f"/api/v1/generations/{result.json()['details']['generation_id']}")
    ).json()
    assert record["status"] == "failed"
    assert record["operation"] is None and record["source"] is None


async def test_metric_date_choices_resolve(client, metric_context, fake):
    output = plan(period=LLMPeriod(kind="previous_month", days=None))
    fake(output, output.model_copy(update={"date_field": "orders.ordered_at"}))
    first = (
        await ask(client, metric_context, question="Last month orders", metric_id="orders_count")
    ).json()
    assert first["status"] == "ambiguous"
    assert first["clarification_questions"][0]["options"] == ["orders.paid_at", "orders.ordered_at"]
    result = await ask(
        client,
        metric_context,
        question="Last month orders",
        metric_id="orders_count",
        **continuation(first, "orders.ordered_at"),
    )
    assert result.status_code == 200, result.text
    assert result.json()["metric_binding"]["time_field"] == "orders.ordered_at"


async def test_multiple_rounds_preserve_answers(client, context, fake):
    provider = fake(
        ambiguous(),
        ambiguous("Which output?", ("id", "region")),
        ok(
            "SELECT id FROM customers WHERE region = :region",
            [LLMParameter(name="region", type="string", value="서울")],
        ),
    )
    first = (await ask(client, context, question="고객 목록")).json()
    payload = continuation(first)
    second = (await ask(client, context, question="고객 목록", **payload)).json()
    next_payload = continuation(second, "id")
    next_payload["clarification_answers"] = (
        payload["clarification_answers"] + next_payload["clarification_answers"]
    )
    result = await ask(client, context, question="고객 목록", **next_payload)
    assert result.json()["status"] == "ok", result.text
    assert len(json.loads(provider.calls[-1][1][0].content)["clarification_answers"]) == 2
    assert next_payload["reference_time"] == payload["reference_time"]


async def test_explicit_metric_option_overrides_answer(client, metric_context, fake):
    fake(plan(period=LLMPeriod(kind="previous_month", days=None)))
    result = await ask(
        client,
        metric_context,
        question="Last month orders",
        metric_id="orders_count",
        date_field="orders.paid_at",
        expected_context_version=1,
        clarification_answers=[
            {"id": "date", "question": "Which date?", "answer": "orders.ordered_at"}
        ],
    )
    assert result.status_code == 200, result.text
    assert result.json()["metric_binding"]["time_field"] == "orders.paid_at"


async def test_missing_clarification_questions_is_provider_failure(client, context, fake):
    fake(not_ok("ambiguous", "Need more information", []))
    response = await ask(client, context)
    assert response.status_code == 502
    assert response.json()["error"] == "llm_error"


def test_structured_question_schema_is_required_for_all_routes():
    from sidewit.generation.dto import LLMGenerationOutput
    from sidewit.generation.metric_plan import LLMHybridPlan, LLMMetricPlan
    from sidewit.generation.providers.gemini import inline_refs
    from sidewit.generation.tool_plan import ToolPlanSelection

    for model in (LLMGenerationOutput, LLMHybridPlan, LLMMetricPlan, ToolPlanSelection):
        schema = inline_refs(model.model_json_schema())
        question = schema["properties"]["clarifications"]["items"]
        assert question["type"] == "object"
        assert set(question["required"]) == {"question", "options"}
        assert question["additionalProperties"] is False


@pytest.mark.parametrize(
    "answer,field",
    [
        ("주문 생성일 기준", "orders.ordered_at"),
        ("orders.paid_at은 사용하지 말고 주문 생성일로", "orders.ordered_at"),
        ("orders.paid_at은 사용하지 마세요", None),
    ],
)
async def test_date_answer_uses_structured_intent_not_mentions(
    client, metric_context, fake, answer, field
):
    fake(plan(period=LLMPeriod(kind="previous_month", days=None), date_field=field))
    result = await ask(
        client,
        metric_context,
        question="Last month orders",
        expected_context_version=1,
        clarification_answers=[{"id": "date", "question": "Which date?", "answer": answer}],
    )
    assert result.status_code == 200, result.text
    body = result.json()
    if field is None:
        assert body["status"] == "ambiguous" and "operation" not in body
    else:
        assert body["metric_binding"]["time_field"] == field


async def test_unrelated_later_answer_does_not_override_date_intent(client, metric_context, fake):
    fake(plan(period=LLMPeriod(kind="previous_month", days=None), date_field="orders.ordered_at"))
    result = await ask(
        client,
        metric_context,
        question="Last month orders",
        expected_context_version=1,
        clarification_answers=[
            {"id": "date", "question": "Which date?", "answer": "orders.ordered_at"},
            {
                "id": "note",
                "question": "Other requirements?",
                "answer": "orders.paid_at은 사용하지 않음",
            },
        ],
    )
    assert result.json()["metric_binding"]["time_field"] == "orders.ordered_at"
