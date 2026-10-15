import copy
from uuid import UUID

import pytest

from sidewit.application.runtime import get_llm_provider
from sidewit.generation.dto import LLMParameter
from sidewit.generation.metric_plan import LLMPeriod
from sidewit.generation.providers.fake import FakeProvider
from sidewit.main import app
from tests.test_metric_contracts import DEFINITION, plan
from tests.tool_urls import tool_path


@pytest.fixture
async def metric_context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "metrics",
            "dialect": "postgresql",
            "timezone": "Asia/Seoul",
            "allow_generated_sql": False,
            "definition": DEFINITION,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def install(*outputs):
    provider = FakeProvider(list(outputs))
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return provider


async def ask(client, context, **kwargs):
    return await client.post(
        "/api/v1/generate",
        json={
            "context_id": context["id"],
            "question": "All order rows",
            "reference_time": "2026-10-02T09:00:00+09:00",
            **kwargs,
        },
    )


async def saved(client, context):
    install(plan())
    generation = (await ask(client, context, metric_id="orders_count")).json()
    draft = (
        await client.post(
            "/api/v1/tools/draft",
            json={
                "generation_id": generation["generation_id"],
                "step_id": generation["operations"][0]["id"],
            },
        )
    ).json()
    res = await client.post(
        "/api/v1/tools",
        json={
            "name": "count_orders",
            "description": "All order rows",
            "context_id": context["id"],
            "input_schema": draft["input_schema"],
            "operation": draft["operation"],
            "source_generation_id": draft["source_generation_id"],
            # Omission must not strip the source's binding.
        },
    )
    assert res.status_code == 201, res.text
    return res.json(), generation


async def test_bound_end_to_end_and_deterministic_invoke(client, metric_context):
    tool, generation = await saved(client, metric_context)
    assert (
        tool["metric_binding"]
        == generation["operations"][0]["metric_binding"]
        == {
            "metric_id": "orders_count",
            "time_field": None,
        }
    )
    detail = (await client.get(f"/api/v1/generations/{generation['generation_id']}")).json()
    assert detail["prompt_version"] == "metric_plan_v3"
    provider = install()  # any call would fail
    first = await client.post(tool_path("count_orders", "/invoke"), json={"arguments": {}})
    second = await client.post(tool_path("count_orders", "/invoke"), json={"arguments": {}})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert "COUNT(*)" in first.json()["operation"]["statement"]
    assert provider.calls == []


async def test_bound_tool_edits_cannot_change_count_or_binding(client, metric_context):
    tool, _ = await saved(client, metric_context)
    body = {k: tool[k] for k in ("description", "input_schema", "operation")}
    body["expected_version"] = tool["version"]
    body["operation"] = {
        "type": "sql",
        "statement": tool["operation"]["statement"].replace("COUNT(*)", 'COUNT("orders"."id")'),
    }
    res = await client.put(tool_path("count_orders"), json=body)
    assert res.status_code == 422
    assert "metric_contract_mismatch" in res.text
    body["operation"] = tool["operation"]
    body["metric_binding"] = None
    assert (await client.put(tool_path("count_orders"), json=body)).status_code == 422


async def test_changing_metric_invalidates_bound_tool(client, metric_context):
    await saved(client, metric_context)
    definition = copy.deepcopy(metric_context["definition"])
    definition["metrics"][0]["aggregate"] = {"kind": "non_null_count", "field": "orders.id"}
    res = await client.put(
        f"/api/v1/contexts/{metric_context['id']}",
        json={
            **{
                k: metric_context[k] for k in ("name", "dialect", "timezone", "allow_generated_sql")
            },
            "definition": definition,
            "expected_version": 1,
        },
    )
    assert res.status_code == 200
    assert res.json()["revalidation"]["invalid"][0]["name"] == "count_orders"
    invoke = await client.post(tool_path("count_orders", "/invoke"), json={"arguments": {}})
    assert invoke.status_code == 409


async def test_failed_plan_repair_never_stores_rejected_sql(client, metric_context):
    provider = install(plan("unknown"), plan())
    res = await ask(client, metric_context)
    assert res.status_code == 200, res.text
    assert len(provider.calls) == 2
    assert "do not return SQL" in provider.calls[1][1][-1].content
    detail = (await client.get(f"/api/v1/generations/{res.json()['generation_id']}")).json()
    assert detail["attempts"] == 2 and detail["status"] == "ok"


async def test_unrepairable_plan_is_invalid_and_cannot_draft(client, metric_context):
    install(plan("unknown"), plan("unknown"))
    res = await ask(client, metric_context)
    assert res.status_code == 422 and "statement" not in res.text
    gid = res.json()["details"]["generation_id"]
    detail = (await client.get(f"/api/v1/generations/{gid}")).json()
    assert detail["operation"] is None and detail["attempts"] == 2
    assert (
        await client.post("/api/v1/tools/draft", json={"generation_id": gid})
    ).status_code == 409


async def test_date_ambiguity_is_server_owned_and_does_not_repair(client, metric_context):
    provider = install(plan(period=LLMPeriod(kind="rolling_days", days=30)))
    res = await ask(client, metric_context, question="Past 30 days order count")
    assert res.json()["status"] == "ambiguous" and "operation" not in res.json()
    assert len(provider.calls) == 1
    provider = install(plan(period=LLMPeriod(kind="rolling_days", days=30)))
    res = await ask(client, metric_context, metric_id="orders_count", date_field="orders.paid_at")
    assert (
        res.json()["operations"][0]["operation"]["parameters"]["start_at"]
        == "2026-09-02T09:00:00+09:00"
    )
    assert len(provider.calls) == 1


async def test_forced_metric_and_period_override(client, metric_context):
    install(
        plan(
            "regional_revenue",
            [LLMParameter(name="region", type="string", value="서울")],
            date_field="orders.paid_at",
        )
    )
    res = await ask(
        client, metric_context, metric_id="regional_revenue", period={"kind": "previous_month"}
    )
    assert res.status_code == 200, res.text
    assert (
        res.json()["operations"][0]["operation"]["parameters"]["start_at"]
        == "2026-09-01T00:00:00+09:00"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric_id": "missing"},
        {"date_field": "orders.paid_at"},
        {"metric_id": "orders_count", "date_field": "orders.id"},
        {"period": {"kind": "rolling_days"}},
        {"period": {"kind": "previous_month", "days": 3}},
    ],
)
async def test_invalid_trusted_options_do_not_call_provider(client, metric_context, kwargs):
    provider = install()
    res = await ask(client, metric_context, **kwargs)
    assert res.status_code == 422 and not provider.calls


async def test_source_binding_cannot_be_replaced(client, metric_context):
    _, generation = await saved(client, metric_context)
    draft = (
        await client.post(
            "/api/v1/tools/draft", json={"generation_id": generation["generation_id"]}
        )
    ).json()
    res = await client.post(
        "/api/v1/tools",
        json={
            "context_id": metric_context["id"],
            "name": "forged",
            "description": "forged",
            "input_schema": draft["input_schema"],
            "operation": draft["operation"],
            "source_generation_id": generation["generation_id"],
            "metric_binding": {"metric_id": "regional_revenue"},
        },
    )
    assert res.status_code == 422


async def test_unknown_contract_rejected_at_context_save(client):
    raw = copy.deepcopy(DEFINITION)
    raw["metrics"][0]["aggregate"] = {"kind": "row_count", "field": "orders.id"}
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "bad",
            "dialect": "postgresql",
            "definition": raw,
        },
    )
    assert res.status_code == 422 and "invalid_metric_contract" in res.text


async def test_old_context_without_metrics_has_no_spurious_version_change(client, store):
    raw = {k: copy.deepcopy(v) for k, v in DEFINITION.items() if k != "metrics"}
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "legacy",
            "dialect": "postgresql",
            "definition": raw,
        },
    )
    context = res.json()
    record = store.context(UUID(context["id"]))
    record.definition = raw
    store.save_context(record)
    res = await client.put(
        f"/api/v1/contexts/{context['id']}",
        json={
            **{k: context[k] for k in ("name", "dialect", "timezone", "allow_generated_sql")},
            "definition": raw,
            "expected_version": context["version"],
        },
    )
    assert res.status_code == 200
    assert res.json()["context"]["version"] == 1
    assert res.json()["revalidation"]["checked"] == 0


async def test_non_ok_metric_plan_cannot_leave_operation_or_binding(client, metric_context):
    output = plan().model_copy(update={"status": "unanswerable", "reason": "unsupported data"})
    install(plan("unknown"), output)
    res = await ask(client, metric_context)
    assert res.status_code == 200 and res.json()["status"] == "unanswerable"
    detail = (await client.get(f"/api/v1/generations/{res.json()['generation_id']}")).json()
    assert detail["operation"] is detail["metric_binding"] is detail["validation"] is None


# allow_generated_sql ---------------------------------------------------------------------


async def put_context(client, context, **changes):
    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    body = {k: current[k] for k in ("name", "dialect", "timezone", "allow_generated_sql")}
    body |= {"definition": current["definition"], "expected_version": current["version"]}
    return await client.put(f"/api/v1/contexts/{context['id']}", json=body | changes)


async def test_disallowing_generated_sql_requires_registered_metrics(client):
    res = await client.post(
        "/api/v1/contexts",
        json={"name": "empty", "dialect": "postgresql", "allow_generated_sql": False},
    )
    assert res.status_code == 422
    assert res.json()["details"]["errors"][0]["code"] == "no_generation_path"


async def test_removing_all_metrics_when_generated_sql_is_disallowed_is_rejected(
    client, metric_context
):
    current = (await client.get(f"/api/v1/contexts/{metric_context['id']}")).json()
    definition = current["definition"] | {"metrics": []}
    res = await put_context(client, metric_context, definition=definition)
    assert res.status_code == 422


async def test_toggling_generated_sql_bumps_version(client, metric_context):
    res = await put_context(client, metric_context, allow_generated_sql=True)
    assert res.status_code == 200
    assert res.json()["context"]["version"] == metric_context["version"] + 1
    assert res.json()["context"]["allow_generated_sql"] is True


async def test_metric_options_without_registered_metrics_are_rejected_before_the_llm(client):
    context = (
        await client.post("/api/v1/contexts", json={"name": "plain", "dialect": "postgresql"})
    ).json()
    provider = install()
    for options in ({"metric_id": "orders_count"}, {"require_metric": True}):
        res = await ask(client, context, **options)
        assert res.status_code == 422 and res.json()["error"] == "invalid_metric_request"
    assert provider.calls == []


# bound tools ----------------------------------------------------------------------------


async def test_invoke_of_a_current_bound_tool_does_not_write(client, metric_context):
    tool, _ = await saved(client, metric_context)
    first = await client.post(tool_path("count_orders", "/invoke"), json={"arguments": {}})
    assert first.status_code == 200
    after = (await client.get(tool_path("count_orders"))).json()
    assert after["updated_at"] == tool["updated_at"]


async def test_refresh_contract_rebuilds_sql_and_keeps_descriptions(client, metric_context):
    await saved(client, metric_context)
    current = (await client.get(f"/api/v1/contexts/{metric_context['id']}")).json()
    definition = copy.deepcopy(current["definition"])
    definition["metrics"][0]["aggregate"] = {"kind": "non_null_count", "field": "orders.id"}
    assert (await put_context(client, metric_context, definition=definition)).status_code == 200
    stale = (await client.get(tool_path("count_orders"))).json()
    assert stale["status"] == "invalid"

    res = await client.post(
        tool_path("count_orders", "/refresh-contract"), json={"expected_version": 1}
    )
    assert res.status_code == 200, res.text
    refreshed = res.json()
    assert refreshed["status"] == "valid"
    assert refreshed["version"] == stale["version"] + 1
    assert "COUNT(orders.id)" in refreshed["operation"]["statement"]
    assert refreshed["metric_binding"] == stale["metric_binding"]
    invoked = await client.post(tool_path("count_orders", "/invoke"), json={"arguments": {}})
    assert invoked.status_code == 200

    again = await client.post(
        tool_path("count_orders", "/refresh-contract"),
        json={"expected_version": refreshed["version"]},
    )
    assert again.json()["version"] == refreshed["version"], "no change, no new version"


async def test_refresh_contract_keeps_input_descriptions(client, metric_context):
    install(
        plan(
            "regional_revenue",
            [LLMParameter(name="region", type="string", value="서울")],
            LLMPeriod(kind="rolling_days", days=7),
            date_field="orders.paid_at",
        )
    )
    generation = (await ask(client, metric_context)).json()
    draft = (
        await client.post(
            "/api/v1/tools/draft", json={"generation_id": generation["generation_id"]}
        )
    ).json()
    draft["input_schema"]["properties"]["region"]["description"] = "Sales region"
    body = {k: draft[k] for k in ("input_schema", "operation", "source_generation_id")}
    body |= {"name": "revenue", "description": "Revenue", "context_id": metric_context["id"]}
    assert (await client.post("/api/v1/tools", json=body)).status_code == 201

    refreshed = (
        await client.post(tool_path("revenue", "/refresh-contract"), json={"expected_version": 1})
    ).json()
    assert refreshed["input_schema"]["properties"]["region"]["description"] == "Sales region"
    assert refreshed["input_schema"]["required"] == ["region", "start_at", "end_at"]


async def test_refresh_contract_requires_a_bound_tool_with_a_registered_metric(
    client, metric_context
):
    tool_body = {
        "name": "manual",
        "description": "Manual",
        "context_id": metric_context["id"],
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "operation": {"type": "sql", "statement": "SELECT COUNT(*) FROM orders"},
    }
    assert (await client.post("/api/v1/tools", json=tool_body)).status_code == 201
    res = await client.post(tool_path("manual", "/refresh-contract"), json={"expected_version": 1})
    assert res.status_code == 409 and res.json()["error"] == "metric_contract_unavailable"

    await saved(client, metric_context)
    current = (await client.get(f"/api/v1/contexts/{metric_context['id']}")).json()
    definition = copy.deepcopy(current["definition"])
    definition["metrics"] = [m for m in definition["metrics"] if m["id"] != "orders_count"]
    assert (await put_context(client, metric_context, definition=definition)).status_code == 200
    res = await client.post(
        tool_path("count_orders", "/refresh-contract"), json={"expected_version": 1}
    )
    assert res.status_code == 409 and "no longer registered" in res.json()["message"]
