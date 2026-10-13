import copy

import pytest

from sidewit.api.generate import get_llm_provider
from sidewit.generation.dto import LLMGenerationOutput, LLMParameter, LLMToolSuggestion
from sidewit.generation.providers.fake import FakeProvider
from sidewit.main import app

DEFINITION = {
    "resources": [
        {
            "name": "customers",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "region", "type": "text"},
            ],
        },
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                {"name": "customer_id", "type": "bigint"},
                {"name": "amount", "type": "numeric"},
                {"name": "status", "type": "text"},
                {"name": "paid_at", "type": "timestamptz"},
            ],
        },
    ],
    "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
    "rules": [{"id": "rule_1", "text": "매출은 COMPLETED 주문만 포함한다."}],
}

SQL = (
    "SELECT SUM(o.amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.region = :region AND o.paid_at >= :start_at AND o.status = 'COMPLETED'"
)
SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "description": "지역"},
        "start_at": {"type": "string", "format": "date-time"},
    },
    "required": ["region", "start_at"],
    "additionalProperties": False,
}
ARGS = {"region": "서울", "start_at": "2026-09-02T00:00:00+09:00"}


class ExplodingProvider:
    name = "exploding"
    model = "none"

    async def generate_structured(self, *args, **kwargs):
        raise AssertionError("invoke must never call an LLM")


@pytest.fixture
async def context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={"name": "commerce", "dialect": "postgresql", "definition": DEFINITION},
    )
    assert res.status_code == 201
    return res.json()


def tool_body(context, **overrides):
    body = {
        "context_id": context["id"],
        "name": "regional_revenue",
        "description": "지역별 기간 매출",
        "input_schema": SCHEMA,
        "operation": {"type": "sql", "statement": SQL},
    }
    return body | overrides


@pytest.fixture
async def tool(client, context):
    res = await client.post("/api/v1/tools", json=tool_body(context))
    assert res.status_code == 201, res.text
    return res.json()


async def invoke(client, name="regional_revenue", arguments=ARGS):
    return await client.post(f"/api/v1/tools/{name}/invoke", json={"arguments": arguments})


async def update_context(client, context, definition):
    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    res = await client.put(
        f"/api/v1/contexts/{context['id']}",
        json={
            "name": current["name"],
            "dialect": current["dialect"],
            "timezone": current["timezone"],
            "allow_generated_sql": current["allow_generated_sql"],
            "definition": definition,
            "expected_version": current["version"],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def renamed_amount():
    definition = copy.deepcopy(DEFINITION)
    orders = definition["resources"][1]
    orders["fields"][2]["name"] = "payment_amount"
    return definition


# create / read / update / delete --------------------------------------------------------


async def test_create_tool(client, context, tool):
    assert tool["name"] == "regional_revenue"
    assert tool["status"] == "valid"
    assert tool["version"] == 1
    assert tool["context_version"] == context["version"]
    assert tool["validation_errors"] == []
    assert tool["operation"] == {"type": "sql", "statement": SQL}
    assert tool["input_schema"] == SCHEMA

    assert (await client.get("/api/v1/tools/regional_revenue")).json() == tool
    listed = (await client.get(f"/api/v1/tools?context_id={context['id']}")).json()["items"]
    assert [t["name"] for t in listed] == ["regional_revenue"]


async def test_tool_may_use_relative_time(client, context):
    body = tool_body(
        context,
        name="recent_revenue",
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
            "additionalProperties": False,
        },
        operation={
            "type": "sql",
            "statement": "SELECT SUM(amount) FROM orders "
            "WHERE paid_at >= NOW() - :days * INTERVAL '1 day'",
        },
    )
    res = await client.post("/api/v1/tools", json=body)
    assert res.status_code == 201, res.text


@pytest.mark.parametrize(
    ("overrides", "expected_codes"),
    [
        (
            {"operation": {"type": "sql", "statement": SQL.replace(":start_at", ":start")}},
            ["undeclared_parameter", "unused_parameter"],
        ),
        (
            {"operation": {"type": "sql", "statement": "SELECT SUM(total) FROM orders"}},
            ["unknown_field", "unused_parameter", "unused_parameter"],
        ),
        (
            {"operation": {"type": "sql", "statement": "DELETE FROM orders"}},
            ["statement_not_allowed"],
        ),
        (
            {"input_schema": SCHEMA | {"required": ["region"]}},
            ["invalid_input_schema"],
        ),
        (
            {
                "input_schema": SCHEMA
                | {
                    "properties": SCHEMA["properties"]
                    | {"region": {"type": "string", "default": "서울"}}
                }
            },
            ["invalid_input_schema"],
        ),
    ],
)
async def test_create_rejects_invalid_tool(client, context, overrides, expected_codes):
    res = await client.post("/api/v1/tools", json=tool_body(context, **overrides))
    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "tool_validation_failed"
    assert [e["code"] for e in body["details"]["validation_errors"]] == expected_codes
    assert (await client.get("/api/v1/tools")).json()["items"] == []


@pytest.mark.parametrize(
    "name", ["RegionalRevenue", "regional-revenue", "1revenue", "_revenue", "r" * 65, ""]
)
async def test_tool_name_must_be_snake_case(client, context, name):
    res = await client.post("/api/v1/tools", json=tool_body(context, name=name))
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"


async def test_duplicate_tool_name(client, context, tool):
    res = await client.post("/api/v1/tools", json=tool_body(context))
    assert res.status_code == 409
    assert res.json()["error"] == "tool_name_conflict"


async def test_create_requires_existing_context(client):
    body = tool_body({"id": "00000000-0000-0000-0000-000000000000"})
    res = await client.post("/api/v1/tools", json=body)
    assert res.status_code == 404


async def test_update_revalidates_and_increments_version(client, context, tool):
    body = {
        "expected_version": tool["version"],
        "description": "지역별 기간 매출 (수정)",
        "input_schema": SCHEMA,
        "operation": {"type": "sql", "statement": SQL + " GROUP BY c.region"},
    }
    res = await client.put("/api/v1/tools/regional_revenue", json=body)
    assert res.status_code == 200
    updated = res.json()
    assert updated["version"] == 2
    assert updated["description"] == "지역별 기간 매출 (수정)"
    assert updated["id"] == tool["id"]

    body["expected_version"] = updated["version"]
    unchanged = await client.put("/api/v1/tools/regional_revenue", json=body)
    assert unchanged.json()["version"] == 2

    bad = body | {"operation": {"type": "sql", "statement": "SELECT nope FROM orders"}}
    res = await client.put("/api/v1/tools/regional_revenue", json=bad)
    assert res.status_code == 422
    current = (await client.get("/api/v1/tools/regional_revenue")).json()
    assert current["version"] == 2
    assert current["operation"] == body["operation"]


async def test_update_cannot_rename_or_move(client, context, tool):
    body = {
        "description": "x",
        "input_schema": SCHEMA,
        "operation": {"type": "sql", "statement": SQL},
        "name": "other",
    }
    res = await client.put("/api/v1/tools/regional_revenue", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"


async def test_delete_tool(client, context, tool):
    assert (await client.delete("/api/v1/tools/regional_revenue")).status_code == 204
    assert (await client.get("/api/v1/tools/regional_revenue")).status_code == 404
    assert (await client.delete("/api/v1/tools/regional_revenue")).status_code == 404


# invoke ---------------------------------------------------------------------------------


async def test_invoke_returns_canonical_operation_without_llm(client, context, tool):
    app.dependency_overrides[get_llm_provider] = lambda: ExplodingProvider()
    res = await invoke(client)
    assert res.status_code == 200
    assert res.json() == {
        "validation": tool["validation"],
        "tool": "regional_revenue",
        "tool_version": 1,
        "context_version": context["version"],
        "operation": {
            "type": "sql",
            "dialect": "postgresql",
            "statement": SQL,
            "parameters": ARGS,
            "parameter_types": {"region": "string", "start_at": "date-time"},
        },
    }
    assert (await invoke(client)).json() == res.json(), "deterministic"


async def test_invoke_parameter_order_follows_required(client, context):
    """Properties may have a different order; `required` defines the public input order."""
    schema = {
        "type": "object",
        "properties": {
            "region": {"type": "string"},
            "start_at": {"type": "string", "format": "date-time"},
        },
        "required": ["start_at", "region"],
        "additionalProperties": False,
    }
    res = await client.post(
        "/api/v1/tools", json=tool_body(context, name="ordered", input_schema=schema)
    )
    assert res.status_code == 201
    assert list(res.json()["input_schema"]["properties"]) == ["region", "start_at"], (
        "precondition: properties differ from required order"
    )
    operation = (await invoke(client, name="ordered", arguments=ARGS)).json()["operation"]
    assert list(operation["parameters"]) == ["start_at", "region"]
    assert list(operation["parameter_types"]) == ["start_at", "region"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"region": "서울"}, [("missing_argument", "start_at")]),
        (ARGS | {"extra": 1}, [("unexpected_argument", "extra")]),
        (ARGS | {"region": 1}, [("argument_type_mismatch", "region")]),
        (ARGS | {"start_at": "2026-09-02"}, [("argument_type_mismatch", "start_at")]),
    ],
)
async def test_invoke_rejects_invalid_arguments(client, context, tool, arguments, expected):
    res = await invoke(client, arguments=arguments)
    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "invalid_arguments"
    assert [(e["code"], e["ref"]) for e in body["details"]["errors"]] == expected


@pytest.mark.parametrize(
    "body", [{"region": "서울"}, {"arguments": ARGS, "metadata": {}}, {"arguments": []}]
)
async def test_invoke_requires_arguments_envelope(client, context, tool, body):
    res = await client.post("/api/v1/tools/regional_revenue/invoke", json=body)
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"


async def test_invoke_unknown_tool(client):
    res = await invoke(client, name="missing")
    assert res.status_code == 404
    assert res.json()["error"] == "tool_not_found"


# context changes and revalidation -------------------------------------------------------


async def test_context_change_invalidates_affected_tool(client, context, tool):
    other = tool_body(
        context,
        name="customer_regions",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        operation={"type": "sql", "statement": "SELECT DISTINCT region FROM customers"},
    )
    assert (await client.post("/api/v1/tools", json=other)).status_code == 201

    result = await update_context(client, context, renamed_amount())
    assert result["context"]["version"] == 2
    revalidation = result["revalidation"]
    assert revalidation["checked"] == 2
    assert revalidation["valid"] == ["customer_regions"]
    assert [t["name"] for t in revalidation["invalid"]] == ["regional_revenue"]
    assert revalidation["invalid"][0]["errors"][0]["code"] == "unknown_field"

    broken = (await client.get("/api/v1/tools/regional_revenue")).json()
    assert broken["status"] == "invalid"
    assert broken["context_version"] == 1, "last version it was valid against"
    assert broken["validation_errors"][0]["ref"] == "orders.amount"

    unaffected = (await client.get("/api/v1/tools/customer_regions")).json()
    assert (unaffected["status"], unaffected["context_version"]) == ("valid", 2)

    res = await invoke(client)
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "tool_invalid"
    assert body["details"]["validation_errors"][0]["code"] == "unknown_field"
    assert "SELECT" not in res.text


async def test_tool_becomes_valid_again_when_context_is_fixed(client, context, tool):
    await update_context(client, context, renamed_amount())
    result = await update_context(client, context, DEFINITION)
    assert result["revalidation"]["valid"] == ["regional_revenue"]
    res = await invoke(client)
    assert res.status_code == 200
    assert res.json()["context_version"] == 3


async def test_invalid_tool_can_be_fixed_by_update(client, context, tool):
    await update_context(client, context, renamed_amount())
    body = {
        "expected_version": tool["version"],
        "description": "지역별 기간 매출",
        "input_schema": SCHEMA,
        "operation": {"type": "sql", "statement": SQL.replace("o.amount", "o.payment_amount")},
    }
    res = await client.put("/api/v1/tools/regional_revenue", json=body)
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["version"], res.json()["context_version"]) == (
        "valid",
        2,
        2,
    )
    assert (await invoke(client)).status_code == 200


async def test_stale_tool_is_revalidated_on_invoke(client, context, tool, store):
    from uuid import UUID

    record = store.context(UUID(context["id"]))
    record.version = 5
    store.save_context(record)

    res = await invoke(client)
    assert res.status_code == 200
    assert res.json()["context_version"] == 5
    assert (await client.get("/api/v1/tools/regional_revenue")).json()["context_version"] == 5


async def test_context_with_tools_cannot_be_deleted(client, context, tool):
    res = await client.delete(f"/api/v1/contexts/{context['id']}")
    assert res.status_code == 409
    assert res.json()["error"] == "context_has_tools"
    assert res.json()["details"] == {"tools": ["regional_revenue"]}

    await client.delete("/api/v1/tools/regional_revenue")
    assert (await client.delete(f"/api/v1/contexts/{context['id']}")).status_code == 204


# draft ----------------------------------------------------------------------------------


def generation_output(status="ok"):
    if status != "ok":
        return LLMGenerationOutput(
            status=status,
            reason="r",
            clarifications=[{"question": "q?", "options": []}] if status == "ambiguous" else [],
            statement=None,
            parameters=[],
            tool_suggestion=None,
        )
    return LLMGenerationOutput(
        status="ok",
        reason=None,
        clarifications=[],
        statement=SQL,
        parameters=[
            LLMParameter(name="region", type="string", value="서울"),
            LLMParameter(name="start_at", type="date-time", value="2026-09-02T00:00:00+09:00"),
        ],
        tool_suggestion=LLMToolSuggestion(name="regional_revenue", description="지역 매출"),
    )


async def generate(client, context, output):
    app.dependency_overrides[get_llm_provider] = lambda: FakeProvider([output])
    res = await client.post(
        "/api/v1/generate", json={"context_id": context["id"], "question": "서울 매출"}
    )
    return res.json()["generation_id"]


@pytest.mark.parametrize("omit_step", [False, True])
async def test_draft_from_generation_is_not_saved(client, context, omit_step):
    generation_id = await generate(client, context, generation_output())
    res = await client.post("/api/v1/tools/draft", json={"generation_id": generation_id})
    assert res.status_code == 200
    draft = res.json()
    assert draft == {
        "metric_binding": None,
        "context_id": context["id"],
        "name": "regional_revenue",
        "description": "지역 매출",
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {"type": "string"},
                "start_at": {"type": "string", "format": "date-time"},
            },
            "required": ["region", "start_at"],
            "additionalProperties": False,
        },
        "operation": {"type": "sql", "statement": SQL},
        "example_arguments": ARGS,
        "source_generation_id": generation_id,
        "source_step_id": "step_1",
    }
    assert (await client.get("/api/v1/tools")).json()["items"] == []

    payload = {k: v for k, v in draft.items() if k != "example_arguments"}
    if omit_step:
        payload.pop("source_step_id")
    saved = await client.post("/api/v1/tools", json=payload)
    assert saved.status_code == 201
    assert saved.json()["source_generation_id"] == generation_id
    assert saved.json()["source_step_id"] == "step_1"
    stored = await client.get(f"/api/v1/tools/{saved.json()['name']}")
    assert stored.json()["source_step_id"] == "step_1"
    assert (await invoke(client, arguments=draft["example_arguments"])).status_code == 200


async def test_draft_is_revalidated_on_save_against_current_context(client, context):
    generation_id = await generate(client, context, generation_output())
    draft = (await client.post("/api/v1/tools/draft", json={"generation_id": generation_id})).json()
    await update_context(client, context, renamed_amount())

    res = await client.post(
        "/api/v1/tools", json={k: v for k, v in draft.items() if k != "example_arguments"}
    )
    assert res.status_code == 422
    assert res.json()["details"]["validation_errors"][0]["code"] == "unknown_field"


@pytest.mark.parametrize("status", ["ambiguous", "unanswerable"])
async def test_draft_requires_ok_generation(client, context, status):
    generation_id = await generate(client, context, generation_output(status))
    res = await client.post("/api/v1/tools/draft", json={"generation_id": generation_id})
    assert res.status_code == 409
    assert res.json()["error"] == "generation_not_ok"


async def test_draft_unknown_generation(client):
    res = await client.post(
        "/api/v1/tools/draft", json={"generation_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert res.status_code == 404


async def test_source_generation_must_belong_to_context(client, context):
    other = await client.post(
        "/api/v1/contexts",
        json={"name": "other", "dialect": "postgresql", "definition": DEFINITION},
    )
    generation_id = await generate(client, other.json(), generation_output())
    res = await client.post(
        "/api/v1/tools", json=tool_body(context, source_generation_id=generation_id)
    )
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_source_generation"


# /validate ------------------------------------------------------------------------------


async def test_validate_sql_with_parameter_types(client, context):
    res = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {
                "statement": "SELECT id FROM orders WHERE paid_at >= NOW() - :d * INTERVAL '1 day'",
                "parameter_types": {"d": "integer"},
                "parameters": {"d": "x"},
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert [e["code"] for e in body["errors"]] == ["parameter_type_mismatch"]
    assert "relative_time" not in body["checks"]


async def test_validate_tool_draft_with_input_schema(client, context):
    res = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {"type": "sql", "statement": SQL},
            "input_schema": SCHEMA | {"required": ["region"]},
        },
    )
    body = res.json()
    assert body["valid"] is False
    assert body["checks"][0] == "input_schema"
    assert [e["code"] for e in body["errors"]] == ["invalid_input_schema"]
    assert (await client.get("/api/v1/tools")).json()["items"] == []


async def test_tools_require_api_key(client, context, tool, api_key):
    assert (await client.get("/api/v1/tools")).status_code == 401
    assert (await invoke(client)).status_code == 401
    assert (await client.post("/api/v1/validate", json={})).status_code == 401


async def test_validate_rejects_parameter_types_alongside_input_schema(client, context):
    res = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {"type": "sql", "statement": SQL, "parameter_types": {"x": "string"}},
            "input_schema": SCHEMA,
        },
    )
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_validate_request"
