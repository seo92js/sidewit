"""Automatic independent Tool plans share normal Tool validation and never expose partial plans."""

from uuid import UUID

import pytest

from sidewit.generation.providers.base import StructuredResult
from sidewit.generation.tool_plan import ToolPlanSelection
from tests.test_generate_api import ask, detail, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_tool_selection import saved_tool


def call(region="서울", name="count_customers"):
    return {
        "tool_name": name,
        "parameters": [{"name": "region", "type": "string", "value": region}],
    }


def plan(calls=None, route="plan", reason=None, clarifications=()):
    return ToolPlanSelection(
        generated_tasks=[],
        route=route,
        calls=calls if calls is not None else [call(), call("부산")],
        reason=reason,
        clarifications=list(clarifications),
    )


async def test_repeated_tool_with_distinct_inputs_returns_validated_independent_plan(
    client, context, fake
):
    tool = await saved_tool(client, context)
    provider = fake(plan())
    response = await ask(client, context, question="서울과 부산 고객 수")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "plan"
    assert body["execution"] == "independent"
    assert "operation" not in body
    assert [s["id"] for s in body["operations"]] == ["step_1", "step_2"]
    assert [s["operation"]["parameters"] for s in body["operations"]] == [
        {"region": "서울"},
        {"region": "부산"},
    ]
    for step in body["operations"]:
        assert step["source"] == "tool"
        assert step["selected_tool"] == {"name": tool["name"], "version": 1}
        assert step["operation"]["statement"] == tool["operation"]["statement"]
        assert step["validation"]["valid"]
    assert len(provider.calls) == 1
    record = await detail(client, body["generation_id"])
    assert record["operations"] == body["operations"]
    assert record["source"] == "plan"
    assert record["operation"] is None
    assert record["prompt_version"] == "tool_plan_v3"
    draft = await client.post("/api/v1/tools/draft", json={"generation_id": body["generation_id"]})
    assert draft.status_code == 409
    assert draft.json()["error"] == "plan_not_draftable"


@pytest.mark.parametrize("count", [1, 5])
async def test_plan_bounds_and_different_tools(client, context, fake, count):
    calls = []
    for i in range(count):
        await saved_tool(client, context, name=f"count_{i}")
        calls.append(call(name=f"count_{i}"))
    fake(plan(calls))
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert len(response.json()["operations"]) == count


@pytest.mark.parametrize(
    "calls,code",
    [
        ([], "invalid_tool_plan"),
        ([call(), call()], "invalid_tool_plan"),
        ([call(), call(name="unknown")], "invalid_tool_selection"),
        ([call(), {"tool_name": "count_customers", "parameters": []}], "invalid_tool_arguments"),
        (
            [
                call(),
                {
                    "tool_name": "count_customers",
                    "parameters": [
                        {"name": "region", "type": "integer", "value": "1"},
                    ],
                },
            ],
            "invalid_tool_arguments",
        ),
    ],
)
async def test_any_bad_step_rejects_entire_plan_without_fallback(
    client, context, fake, calls, code
):
    await saved_tool(client, context)
    provider = fake(plan(calls), ok())
    response = await ask(client, context)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == code
    assert "operations" not in body
    assert "SELECT" not in response.text
    assert len(provider.calls) == 1
    record = await detail(client, body["details"]["generation_id"])
    assert record["status"] == "failed"
    assert record["operations"] is None
    assert record["operation"] is None


async def test_default_clarifications_pin_reference_context(client, context, fake):
    await saved_tool(client, context)
    provider = fake(
        plan(
            [],
            "ambiguous",
            "Which regions?",
            [
                {"question": "Regions?", "options": ["서울 and 부산"]},
            ],
        ),
        plan(),
    )
    first = (await ask(client, context)).json()
    q = first["clarification_questions"][0]
    second = await ask(
        client,
        context,
        reference_time=first["reference_time"],
        expected_context_version=first["context_version"],
        clarification_answers=[
            {"id": q["id"], "question": q["question"], "answer": "서울 and 부산"}
        ],
    )
    assert second.status_code == 200, second.text
    assert second.json()["source"] == "plan"
    assert len(provider.calls) == 2


@pytest.mark.parametrize("mode", ["single", "tool_plan", "auto"])
async def test_removed_modes_are_rejected_before_provider(client, context, fake, mode):
    provider = fake()
    response = await ask(client, context, mode=mode)
    assert response.status_code == 422
    assert not provider.calls


@pytest.mark.parametrize(
    "route,calls,reason,questions",
    [
        ("plan", [call()], "unexpected", []),
        ("unanswerable", [call()], "missing", []),
        ("unanswerable", [], "missing", [{"question": "Q?", "options": []}]),
        ("ambiguous", [], " ", [{"question": "Q?", "options": []}]),
        ("ambiguous", [], "missing", []),
    ],
)
async def test_inconsistent_provider_routes_reject_no_partial_plan(
    client, context, fake, route, calls, reason, questions
):
    await saved_tool(client, context)
    fake(plan(calls, route, reason, questions))
    response = await ask(client, context)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_tool_plan"


async def test_context_change_during_selection_returns_conflict(client, context, fake, store):
    await saved_tool(client, context)
    provider = fake()

    async def select(*args):
        record = store.context(UUID(context["id"]))
        record.version += 1
        store.save_context(record)
        return StructuredResult(parsed=plan())

    provider.generate_structured = select
    response = await ask(client, context)
    assert response.status_code == 409
    assert response.json()["error"] == "context_changed"


async def test_changed_second_tool_returns_conflict_without_first_operation(client, context, fake):
    await saved_tool(client, context)
    second = await saved_tool(client, context, name="other")
    provider = fake()

    async def select(*args):
        update = await client.put(
            "/api/v1/tools/other",
            json={
                "description": "Changed",
                "expected_version": second["version"],
                "input_schema": second["input_schema"],
                "operation": second["operation"],
            },
        )
        assert update.status_code == 200
        return StructuredResult(parsed=plan([call(), call(name="other")]))

    provider.generate_structured = select
    response = await ask(client, context)
    assert response.status_code == 409
    assert response.json()["error"] == "tool_changed"
    assert response.json()["details"]["step_id"] == "step_2"
    record = await detail(client, response.json()["details"]["generation_id"])
    assert record["operations"] is None


async def test_invalid_second_tool_does_not_expose_first_step(client, context, fake, store):
    await saved_tool(client, context)
    other = await saved_tool(client, context, name="other")
    record = store.tool(other["name"])
    record.operation = {"type": "sql", "statement": "SELECT pg_sleep(1)"}
    store.save_tool(record)
    fake(plan([call(), call(name="other")]))
    response = await ask(client, context)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "tool_invalid"
    assert response.json()["details"]["step_id"] == "step_2"
    history = await detail(client, response.json()["details"]["generation_id"])
    assert history["operations"] is None
    assert history["operation"] is None


def test_provider_schema_forbids_more_than_five_calls_and_dependencies():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        plan([call(str(i)) for i in range(6)])
    bad_call = call() | {"depends_on": "step_1"}
    with pytest.raises(ValidationError):
        plan([bad_call])


async def test_stale_continuation_stops_before_provider(client, context, fake):
    await saved_tool(client, context)
    provider = fake()
    response = await ask(client, context, expected_context_version=999)
    assert response.status_code == 409
    assert not provider.calls


async def test_plan_provider_failure_does_not_fall_back(client, context, fake):
    await saved_tool(client, context)
    provider = fake(RuntimeError("provider failed"), ok())
    response = await ask(client, context)
    assert response.status_code == 502
    assert len(provider.calls) == 1
    record = await detail(client, response.json()["details"]["generation_id"])
    assert record["operations"] is None


async def test_tool_in_another_context_cannot_be_selected(client, context, fake):
    await saved_tool(client, context)
    other = (
        await client.post(
            "/api/v1/contexts",
            json={
                "name": "other",
                "dialect": "postgresql",
                "definition": context["definition"],
            },
        )
    ).json()
    await saved_tool(client, other, name="other_context_tool")
    fake(plan([call(), call(name="other_context_tool")]))
    response = await ask(client, context)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_tool_selection"


async def test_default_without_tools_uses_existing_generation(client, context, fake):
    provider = fake(ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "generated_sql"
    assert len(provider.calls) == 1


async def test_default_no_complete_match_generates_whole_question(client, context, fake):
    await saved_tool(client, context)
    provider = fake(plan([], "generate"), ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "generated_sql"
    assert "operations" not in response.json()
    assert len(provider.calls) == 2
    assert "generate: if no saved Tool fits" in provider.calls[0][0]
    record = await detail(client, response.json()["generation_id"])
    assert record["prompt_version"].startswith("tool_plan_v3+")


@pytest.mark.parametrize(
    "payload",
    [
        {"calls": [call()]},
        {"reason": "partial answer"},
        {"clarifications": [{"question": "Region?", "options": []}]},
    ],
)
async def test_fallback_cannot_smuggle_partial_plan(client, context, fake, payload):
    await saved_tool(client, context)
    selection = ToolPlanSelection.model_validate(plan([], "generate").model_dump() | payload)
    provider = fake(selection, ok())
    response = await ask(client, context)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_tool_plan"
    assert len(provider.calls) == 1


async def test_default_fallback_keeps_repair_budget(client, context, fake):
    await saved_tool(client, context)
    provider = fake(plan([], "generate"), ok("SELECT missing FROM customers", []), ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert len(provider.calls) == 3


async def test_default_selects_different_operations(client, context, fake):
    await saved_tool(client, context)
    response = await client.post(
        "/api/v1/tools",
        json={
            "context_id": context["id"],
            "name": "list_customers",
            "description": "List customer IDs.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "operation": {"type": "sql", "statement": "SELECT id FROM customers LIMIT 5"},
        },
    )
    assert response.status_code == 201, response.text
    fake(plan([call(), {"tool_name": "list_customers", "parameters": []}]))
    response = await ask(client, context, question="서울 고객 수와 고객 ID 다섯 개")
    assert response.status_code == 200, response.text
    steps = response.json()["operations"]
    assert [s["selected_tool"]["name"] for s in steps] == ["count_customers", "list_customers"]
    assert steps[0]["operation"]["statement"] != steps[1]["operation"]["statement"]
    assert steps[1]["operation"]["parameters"] == {}
