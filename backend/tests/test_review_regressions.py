"""Generation snapshots, public diagnostics and request encoding must fail safely."""

import copy
import json

import pytest

from sidewit.generation.metric_plan import LLMPeriod
from sidewit.validation.input_schema import input_schema_for
from tests.test_generate_api import ask, detail, not_ok, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_metric_api import install as install_metric
from tests.test_metric_api import metric_context as metric_context
from tests.test_metric_contracts import plan as metric_plan
from tests.test_mixed_plan import mixed
from tests.test_tool_selection import choice, saved_tool


@pytest.mark.parametrize("route", ["sql", "repair", "fallback", "metric", "ambiguous"])
async def test_unpinned_generation_rejects_context_changed_while_waiting(
    client, context, metric_context, fake, route
):
    current = metric_context if route == "metric" else context
    output = ok("SELECT id FROM customers", [])
    if route == "metric":
        outputs = [metric_plan()]
    elif route == "repair":
        outputs = [ok("SELECT missing FROM customers", []), output]
    elif route == "fallback":
        await saved_tool(client, context)
        outputs = [choice("generate"), output]
    elif route == "ambiguous":
        outputs = [not_ok("ambiguous", "Choose a region.", ["Which region?"])]
    else:
        outputs = [output]
    provider = fake(*outputs)
    original = provider.generate_structured

    async def changed(*args):
        result = await original(*args)
        if len(provider.calls) == len(outputs):
            body = {
                k: current[k]
                for k in ("name", "dialect", "timezone", "allow_generated_sql", "definition")
            }
            body.update(name="changed-context", expected_version=current["version"])
            updated = await client.put(f"/api/v1/contexts/{current['id']}", json=body)
            assert updated.status_code == 200, updated.text
        return result

    provider.generate_structured = changed
    response = await ask(client, current)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "context_changed"
    record = await detail(client, response.json()["details"]["generation_id"])
    assert record["status"] == "failed"
    assert record["operation"] is record["operations"] is record["validation"] is None
    assert record["source"] is None
    draft = await client.post("/api/v1/tools/draft", json={"generation_id": record["id"]})
    assert draft.status_code == 409


PRIVATE_VALUE = "REVIEW_PRIVATE_VALUE"
REJECTED_SQL = [
    f"SELECT id FROM customers WHERE region = '{PRIVATE_VALUE}' AND",
    f"SELECT CAST(lower('{PRIVATE_VALUE}') AS timestamp)",
    f"SELECT ':region {PRIVATE_VALUE}'",
    f'SELECT id FROM "SELECT {PRIVATE_VALUE}"',
    f"SELECT 1 WHERE '{PRIVATE_VALUE}' = '{PRIVATE_VALUE}'",
]


@pytest.mark.parametrize("statement", REJECTED_SQL)
@pytest.mark.parametrize("mixed_plan", [False, True])
async def test_rejected_sql_stays_in_history_not_public_diagnostics(
    client, context, fake, statement, mixed_plan
):
    outputs = [ok(statement, []), ok(statement, [])]
    if mixed_plan:
        await saved_tool(client, context)
        outputs.insert(0, mixed())
    provider = fake(*outputs)
    response = await ask(client, context, question=f"List customers for {PRIVATE_VALUE}")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "generation_invalid"
    assert PRIVATE_VALUE not in response.text and "SELECT" not in response.text
    assert body["details"]["validation_errors"]
    if mixed_plan:
        assert body["details"]["step_id"] == "step_2"
    record = await detail(client, body["details"]["generation_id"])
    assert record["operation"]["statement"] == statement
    assert record["status"] == "failed"
    assert len(provider.calls) == len(outputs)


@pytest.mark.parametrize(
    "reference_time,timezone",
    [
        ("9999-12-31T23:59:59-01:00", "UTC"),
        ("0001-01-01T00:00:00+01:00", "UTC"),
        ("9999-12-31T23:59:59Z", "Asia/Seoul"),
        ("0001-01-01T00:00:00Z", "America/New_York"),
    ],
)
async def test_reference_time_conversion_overflow_is_rejected_before_provider(
    client, context, fake, reference_time, timezone
):
    provider = fake()
    body = {
        k: context[k] for k in ("name", "dialect", "timezone", "allow_generated_sql", "definition")
    }
    body.update(timezone=timezone, expected_version=context["version"])
    updated = await client.put(f"/api/v1/contexts/{context['id']}", json=body)
    assert updated.status_code == 200
    response = await ask(client, context, reference_time=reference_time)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_reference_time"
    assert provider.calls == []


@pytest.mark.parametrize("value", ["\ud800", "\udfff", {"nested": ["\ud800"]}])
async def test_invalid_unicode_tool_arguments_are_client_errors(client, context, value):
    tool = await saved_tool(client, context)
    response = await client.post(
        f"/api/v1/tools/{tool['id']}/invoke",
        content=json.dumps({"arguments": {"region": value}}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_arguments"
    valid = await client.post(
        f"/api/v1/tools/{tool['id']}/invoke", json={"arguments": {"region": "서울 🔑"}}
    )
    assert valid.status_code == 200
    assert valid.json()["operation"]["parameters"] == {"region": "서울 🔑"}


@pytest.mark.parametrize("part", ["description", "enum", "keyword", "input_name", "required"])
async def test_invalid_unicode_input_schema_does_not_write_a_tool(client, context, store, part):
    schema = input_schema_for({"region": "string"})
    if part == "description":
        schema["properties"]["region"]["description"] = "\ud800"
    elif part == "enum":
        schema["properties"]["region"]["enum"] = ["\ud800"]
    elif part == "keyword":
        schema["properties"]["region"]["\ud800"] = "ignored"
    elif part == "input_name":
        schema["properties"]["\ud800"] = {"type": "string"}
    else:
        schema["required"].append("\ud800")
    body = {
        "context_id": context["id"],
        "name": "bad_unicode",
        "description": "Review",
        "operation": {"statement": "SELECT id FROM customers WHERE region = :region"},
        "input_schema": schema,
    }
    response = await client.post(
        "/api/v1/tools", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "tool_validation_failed"
    assert store.tools == {}
    assert list((store.root / "tools").iterdir()) == []


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize(
    "location",
    ["declared_name", "declared_name_only", "value_name", "value", "nested_name", "nested_value"],
)
async def test_validate_rejects_malformed_unicode_parameter_without_500(
    client, context, surrogate, location
):
    operation = {
        "statement": "SELECT id FROM customers WHERE region = :region",
        "parameter_types": {"region": "string"},
        "parameters": {"region": "서울"},
    }
    if location in {"declared_name", "declared_name_only"}:
        operation["parameter_types"][surrogate] = "string"
        if location == "declared_name_only":
            operation.pop("parameters")
    elif location == "value_name":
        operation["parameters"][surrogate] = "ignored"
    elif location == "value":
        operation["parameters"]["region"] = surrogate
    elif location == "nested_name":
        operation["parameters"]["region"] = {"nested": [{surrogate: "ignored"}]}
    else:
        operation["parameters"]["region"] = {"nested": [surrogate]}
    response = await client.post(
        "/api/v1/validate",
        content=json.dumps({"context_id": context["id"], "operation": operation}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert not response.json()["valid"]
    assert response.json()["errors"][0]["code"] == "parameter_type_mismatch"
    # Error messages and references must not echo strings that cannot be serialized.
    assert surrogate not in response.text and "\\ud" not in response.text


async def test_validate_accepts_valid_non_ascii_parameter_values(client, context):
    response = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {
                "statement": "SELECT id FROM customers WHERE region = :region",
                "parameter_types": {"region": "string"},
                "parameters": {"region": "서울 🔑"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["valid"]


async def test_invalid_unicode_schema_update_keeps_saved_contract(client, context, store):
    tool = await saved_tool(client, context)
    schema = copy.deepcopy(tool["input_schema"])
    schema["properties"]["region"]["description"] = "\ud800"
    response = await client.put(
        f"/api/v1/tools/{tool['id']}",
        content=json.dumps(
            {
                "expected_version": tool["version"],
                "description": tool["description"],
                "operation": tool["operation"],
                "input_schema": schema,
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    current = await client.get(f"/api/v1/tools/{tool['id']}")
    assert current.json() == tool
    assert store.healthy()


async def test_metric_prompt_and_compiler_use_the_same_context_local_day(client, metric_context):
    provider = install_metric(
        metric_plan(period=LLMPeriod(kind="previous_day", days=None), date_field="orders.paid_at")
    )
    response = await ask(client, metric_context, reference_time="2026-10-20T23:00:00Z")
    assert response.status_code == 200, response.text
    assert "2026-10-21T08:00:00+09:00" in provider.calls[0][0]
    values = response.json()["operations"][0]["operation"]["parameters"]
    assert values == {
        "start_at": "2026-10-20T00:00:00+09:00",
        "end_at": "2026-10-21T00:00:00+09:00",
    }
