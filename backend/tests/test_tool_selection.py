import uuid

import pytest

from sidewit.generation.dto import LLMParameter
from sidewit.generation.providers.base import StructuredResult
from sidewit.generation.tool_plan import ToolPlanSelection, plan_prompt
from tests.test_generate_api import ask, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_metric_api import metric_context as metric_context


async def saved_tool(client, context, name="count_customers"):
    response = await client.post(
        "/api/v1/tools",
        json={
            "context_id": context["id"],
            "name": name,
            "description": "Count all customers in a region, including inactive customers.",
            "input_schema": {
                "type": "object",
                "properties": {"region": {"type": "string"}},
                "required": ["region"],
                "additionalProperties": False,
            },
            "operation": {
                "type": "sql",
                "statement": "SELECT COUNT(*) FROM customers WHERE region = :region",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def choice(route="tool", name="count_customers", parameters=None, **kwargs):
    return ToolPlanSelection(
        route="tools" if route == "tool" else route,
        calls=[
            {
                "tool_name": name,
                "parameters": parameters
                if parameters is not None
                else [LLMParameter(name="region", type="string", value="서울")],
            }
        ]
        if route == "tool"
        else [],
        reason=kwargs.get("reason"),
        clarifications=[
            {"question": q, "options": []} if isinstance(q, str) else q
            for q in kwargs.get("clarifications", [])
        ],
    )


async def test_reuses_exact_saved_sql_and_reports_provenance(client, context, fake):
    tool = await saved_tool(client, context)
    provider = fake(choice())
    response = await ask(client, context, question="서울 고객 수")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "plan"
    step = body["operations"][0]
    assert step["selected_tool"] == {"name": tool["name"], "version": 1}
    assert step["operation"]["statement"] == tool["operation"]["statement"]
    assert step["operation"]["parameters"] == {"region": "서울"}
    assert len(provider.calls) == 1
    history = (await client.get(f"/api/v1/generations/{body['generation_id']}")).json()
    assert history["operations"] == body["operations"]
    assert history["source"] == "plan"
    assert history["attempts"] == 1


async def test_no_match_falls_back_to_existing_generation(client, context, fake):
    await saved_tool(client, context)
    provider = fake(choice("generate"), ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "generated_sql"
    assert len(provider.calls) == 2


async def test_no_match_keeps_full_sql_repair_budget(client, context, fake):
    await saved_tool(client, context)
    provider = fake(choice("generate"), ok("SELECT missing FROM customers", []), ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert len(provider.calls) == 3


async def test_missing_intent_returns_clarification_not_generated_sql(client, context, fake):
    await saved_tool(client, context)
    provider = fake(
        choice("ambiguous", reason="Region is missing", clarifications=["Which region?"])
    )
    response = await ask(client, context, question="고객 수")
    assert response.status_code == 200
    assert response.json()["status"] == "ambiguous"
    assert "operation" not in response.json()
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "selection,code",
    [
        (choice(name="unknown"), "invalid_tool_selection"),
        (choice(parameters=[]), "invalid_tool_arguments"),
        (
            choice(parameters=[LLMParameter(name="region", type="integer", value="1")]),
            "invalid_tool_arguments",
        ),
        (
            choice(parameters=[LLMParameter(name="region", type="string", value="서울")] * 2),
            "invalid_tool_arguments",
        ),
        (choice("ambiguous"), "invalid_tool_plan"),
    ],
)
async def test_bad_selection_fails_without_fallback(client, context, fake, selection, code):
    await saved_tool(client, context)
    provider = fake(selection, ok())
    response = await ask(client, context)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == code
    assert len(provider.calls) == 1
    generation_id = response.json()["details"]["generation_id"]
    detail = (await client.get(f"/api/v1/generations/{generation_id}")).json()
    assert detail["status"] == "failed"
    assert detail["reason"].startswith(code + ":")
    assert detail["tool_selection"] == selection.model_dump(mode="json")
    assert detail["attempts"] == 1
    assert detail["operation"] is None
    assert detail["source"] is None
    draft = await client.post("/api/v1/tools/draft", json={"generation_id": generation_id})
    assert draft.status_code == 409


async def test_other_context_tools_are_not_candidates(client, context, fake):
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
    await saved_tool(client, other)
    provider = fake(ok())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert len(provider.calls) == 1
    assert "count_customers" not in provider.calls[0][0]


@pytest.mark.parametrize("change", ["edit_tool", "delete_tool", "edit_context", "delete_context"])
async def test_changes_during_selection_do_not_invoke_new_contract(
    client, context, fake, store, change
):
    tool = await saved_tool(client, context)
    provider = fake()

    async def select(*args):
        current = store.tool(tool["name"])
        if change == "edit_tool":
            current.version += 1
            store.save_tool(current)
        elif change == "delete_tool":
            store.delete_tool(current)
        elif change == "edit_context":
            changed = store.context(uuid.UUID(context["id"]))
            changed.version += 1
            store.save_context(changed)
        else:
            await client.delete(f"/api/v1/tools/{tool['name']}")
            await client.delete(f"/api/v1/contexts/{context['id']}")
        return StructuredResult(parsed=choice())

    provider.generate_structured = select
    response = await ask(client, context)
    assert response.status_code == (404 if change == "delete_context" else 409), response.text
    if change != "delete_context":
        generation_id = response.json()["details"]["generation_id"]
        detail = (await client.get(f"/api/v1/generations/{generation_id}")).json()
        assert detail["status"] == "failed"
        assert detail["operation"] is None
        assert detail["tool_selection"]["calls"][0]["tool_name"] == tool["name"]
    else:
        assert "generation_id" not in response.json()["details"]
        assert (await client.get("/api/v1/generations")).json()["items"] == []


async def test_invalid_tool_does_not_trigger_generation(client, context, fake, store):
    tool = await saved_tool(client, context)
    record = store.tool(tool["name"])
    record.operation["statement"] = "SELECT missing FROM absent"
    record.version += 1
    record.status = "invalid"
    record.validation = None
    store.save_tool(record)
    provider = fake(choice(), ok())
    response = await ask(client, context)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "tool_invalid"
    assert len(provider.calls) == 1


async def test_selection_provider_failure_does_not_fall_back(client, context, fake):
    await saved_tool(client, context)
    provider = fake(RuntimeError("unavailable"), ok())
    response = await ask(client, context)
    assert response.status_code == 502
    assert len(provider.calls) == 1


async def test_explicit_metric_options_skip_tool_selection(client, metric_context, fake):
    from tests.test_metric_api import ask as metric_ask
    from tests.test_metric_api import saved

    context = metric_context
    await saved(client, context)
    from tests.test_metric_contracts import plan

    provider = fake(plan())
    response = await metric_ask(client, context, metric_id="orders_count")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "metric"
    assert len(provider.calls) == 1


async def test_selected_metric_tool_preserves_binding(client, metric_context, fake):
    from tests.test_metric_api import ask as metric_ask
    from tests.test_metric_api import saved

    context = metric_context
    await saved(client, context)
    provider = fake(choice(name="count_orders", parameters=[]))
    response = await metric_ask(client, context)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "plan"
    assert response.json()["operations"][0]["metric_binding"]["metric_id"] == "orders_count"
    assert len(provider.calls) == 1


async def test_catalog_over_limit_fails_before_llm_call(client, context, fake, monkeypatch):
    await saved_tool(client, context)
    monkeypatch.setattr("sidewit.generation.tool_selection.MAX_CATALOG_CHARS", 1)
    provider = fake(ok())
    response = await ask(client, context)
    assert response.status_code == 422
    assert response.json()["error"] == "tool_catalog_too_large"
    assert not provider.calls
    assert "generation_id" not in response.json()["details"]


async def test_enum_violation_does_not_fall_back(client, context, fake):
    tool = await saved_tool(client, context)
    tool["input_schema"]["properties"]["region"]["enum"] = ["서울"]
    response = await client.put(
        f"/api/v1/tools/{tool['name']}",
        json={
            "description": tool["description"],
            "input_schema": tool["input_schema"],
            "operation": tool["operation"],
            "expected_version": tool["version"],
        },
    )
    assert response.status_code == 200
    provider = fake(
        choice(parameters=[LLMParameter(name="region", type="string", value="부산")]), ok()
    )
    response = await ask(client, context)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_tool_arguments"
    assert len(provider.calls) == 1


async def test_failed_selection_records_usage_once_without_logging_inputs(
    client, context, fake, history, caplog
):
    import logging

    await saved_tool(client, context)
    provider = fake()
    selection = choice(parameters=[LLMParameter(name="region", type="integer", value="932847")])

    async def select(*args):
        return StructuredResult(parsed=selection, input_tokens=123, output_tokens=45)

    provider.generate_structured = select
    with caplog.at_level(logging.INFO, logger="sidewit.generation.history"):
        response = await ask(client, context, question="REVIEW_PRIVATE_QUESTION")
    assert response.status_code == 422
    generation_id = response.json()["details"]["generation_id"]
    records = history.recent(None, 10)
    assert len(records) == 1
    record = records[0]
    assert str(record.id) == generation_id
    assert (record.input_tokens, record.output_tokens) == (123, 45)
    assert record.latency_ms >= 0
    assert record.prompt_version == "tool_plan_v2"
    assert "REVIEW_PRIVATE_QUESTION" not in caplog.text
    assert "932847" not in caplog.text
    assert f"generation id={generation_id}" in caplog.text


async def test_selection_reference_time_is_in_context_timezone(client, context, fake):
    from datetime import datetime

    from sidewit.generation.planners import GenerationInput
    from sidewit.store import ContextRecord

    record = ContextRecord(
        name="local",
        dialect="postgresql",
        timezone="Asia/Seoul",
        definition=context["definition"],
    )
    request = GenerationInput(
        question="어제",
        reference_time=datetime.fromisoformat("2026-10-12T23:30:00+00:00"),
    )
    prompt = plan_prompt([], record, request)
    assert "Reference time: 2026-10-13T08:30:00+09:00" in prompt
