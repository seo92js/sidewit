"""Tool activation is independent of validation and is enforced on all invocation paths."""

import json

import pytest

import sidewit.store as storage
from sidewit.generation.providers.base import StructuredResult
from sidewit.generation.tool_plan import ToolPlanSelection
from sidewit.store import Store
from tests.test_generate_api import ask, ok
from tests.test_generate_api import fake as fake
from tests.test_tool_plan import call, plan
from tests.test_tool_selection import saved_tool
from tests.test_tools_api import ARGS, renamed_amount, tool_body, update_context
from tests.test_tools_api import context as context
from tests.test_tools_api import tool as tool


async def toggle(client, tool, enabled, expected_version=None):
    return await client.patch(
        f"/api/v1/tools/{tool['name']}",
        json={
            "enabled": enabled,
            "expected_version": tool["version"] if expected_version is None else expected_version,
        },
    )


async def test_disable_preserves_contract_and_blocks_invoke(client, tool):
    assert tool["enabled"] is True
    response = await toggle(client, tool, False)
    assert response.status_code == 200, response.text
    disabled = response.json()
    assert disabled["version"] == tool["version"] + 1
    assert disabled["enabled"] is False
    for field in (
        "operation",
        "input_schema",
        "description",
        "metric_binding",
        "status",
        "validation",
        "source_generation_id",
        "source_step_id",
    ):
        assert disabled[field] == tool[field]
    assert (await client.get("/api/v1/tools")).json()["items"][0]["enabled"] is False
    invoked = await client.post(f"/api/v1/tools/{tool['name']}/invoke", json={"arguments": ARGS})
    assert invoked.status_code == 409
    assert invoked.json()["error"] == "tool_disabled"
    assert "operation" not in invoked.json()
    enabled = await toggle(client, disabled, True)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["version"] == disabled["version"] + 1
    assert (
        await client.post(f"/api/v1/tools/{tool['name']}/invoke", json={"arguments": ARGS})
    ).status_code == 200


async def test_activation_is_versioned_and_noop_is_not_a_write(client, tool):
    same = await toggle(client, tool, True)
    assert same.json() == tool
    changed = (await toggle(client, tool, False)).json()
    conflict = await toggle(client, tool, False)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "version_conflict"
    assert conflict.json()["details"]["current_version"] == changed["version"]
    assert (await toggle(client, changed, False)).json() == changed


@pytest.mark.parametrize("committed", [False, True])
async def test_failed_activation_requires_reading_persisted_state(
    client, tool, monkeypatch, committed
):
    def fail(*args):
        raise OSError("simulated storage failure")

    with monkeypatch.context() as patch:
        if committed:
            patch.setattr(storage, "_sync_directory", fail)
        else:
            patch.setattr(storage.os, "replace", fail)
        response = await toggle(client, tool, False)
    assert response.status_code == 503
    assert response.json()["error"] == "storage_unavailable"
    current = (await client.get(f"/api/v1/tools/{tool['name']}")).json()
    assert current["enabled"] is not committed
    assert current["version"] == tool["version"] + int(committed)
    # The current version, rather than the failed request's version, makes retry safe.
    assert (await toggle(client, current, True)).status_code == 200


async def test_disabled_tool_can_be_edited_without_being_enabled(client, tool):
    disabled = (await toggle(client, tool, False)).json()
    response = await client.put(
        f"/api/v1/tools/{tool['name']}",
        json={
            "expected_version": disabled["version"],
            "description": "Edited while disabled",
            "input_schema": tool["input_schema"],
            "operation": tool["operation"],
        },
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["status"] == "valid"


async def test_disabled_invalid_tool_requires_repair_before_enable(client, context, tool):
    disabled = (await toggle(client, tool, False)).json()
    await update_context(client, context, renamed_amount())
    invalid = (await client.get(f"/api/v1/tools/{tool['name']}")).json()
    assert invalid["status"] == "invalid" and invalid["enabled"] is False
    failed = await toggle(client, invalid, True)
    assert failed.status_code == 422
    assert failed.json()["error"] == "tool_validation_failed"
    unchanged = (await client.get(f"/api/v1/tools/{tool['name']}")).json()
    assert unchanged["enabled"] is False
    assert unchanged["version"] == disabled["version"]
    fixed = await client.put(
        f"/api/v1/tools/{tool['name']}",
        json={
            "expected_version": invalid["version"],
            "description": tool["description"],
            "input_schema": tool["input_schema"],
            "operation": {
                "type": "sql",
                "statement": tool["operation"]["statement"].replace("o.amount", "o.payment_amount"),
            },
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["enabled"] is False
    assert (await toggle(client, fixed.json(), True)).json()["enabled"] is True


async def test_invalid_tool_can_still_be_disabled(client, context, tool):
    await update_context(client, context, renamed_amount())
    invalid = (await client.get(f"/api/v1/tools/{tool['name']}")).json()
    assert invalid["status"] == "invalid"
    disabled = await toggle(client, invalid, False)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "invalid"
    assert disabled.json()["enabled"] is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, {}, []])
async def test_activation_requires_a_boolean(client, tool, value):
    response = await toggle(client, tool, value)
    assert response.status_code == 422
    assert (await client.get(f"/api/v1/tools/{tool['name']}")).json()["enabled"] is True


async def test_create_disabled_and_file_restart(client, context, store, tmp_path):
    created = await client.post("/api/v1/tools", json=tool_body(context, enabled=False))
    assert created.status_code == 201
    assert created.json()["enabled"] is False
    store.close()
    reopened = Store(tmp_path / "data")
    try:
        assert reopened.tool(created.json()["name"]).enabled is False
    finally:
        reopened.close()


async def test_older_tool_files_default_to_enabled(client, tool, store, tmp_path):
    path = tmp_path / "data" / "tools" / f"{tool['name']}.json"
    payload = json.loads(path.read_text())
    payload.pop("enabled")
    path.write_text(json.dumps(payload))
    store.restore()
    assert (await client.get(f"/api/v1/tools/{tool['name']}")).json()["enabled"] is True


async def test_disabled_catalog_entries_are_not_sent_to_model(client, context, fake):
    active = await saved_tool(client, context)
    disabled = await saved_tool(client, context, name="disabled_count")
    await toggle(client, disabled, False)
    provider = fake(plan([call(name=active["name"])]))
    response = await ask(client, context, question="서울 고객 수")
    assert response.status_code == 200, response.text
    assert response.json()["operations"][0]["selected_tool"]["name"] == active["name"]
    assert "disabled_count" not in provider.calls[0][0]


async def test_all_disabled_skips_selection_and_generates_normally(client, context, fake):
    tool = await saved_tool(client, context)
    await toggle(client, tool, False)
    provider = fake(ok("SELECT COUNT(*) FROM customers", [], suggestion=False))
    response = await ask(client, context, question="고객 수")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "generated_sql"
    assert len(provider.calls) == 1


async def test_disable_during_selection_rejects_old_plan(client, context, fake):
    tool = await saved_tool(client, context)
    provider = fake()

    async def changed(*args):
        await toggle(client, tool, False)
        return StructuredResult(parsed=plan([call(name=tool["name"])]))

    provider.generate_structured = changed
    response = await ask(client, context, question="서울 고객 수")
    assert response.status_code == 409
    assert response.json()["error"] == "tool_changed"
    assert "operations" not in response.json()


async def test_disable_during_mixed_generation_rejects_partial_plan(client, context, fake):
    tool = await saved_tool(client, context)
    provider = fake()
    calls = 0

    async def changed(system, messages, schema):
        nonlocal calls
        calls += 1
        if schema is ToolPlanSelection:
            return StructuredResult(
                parsed=plan([call(name=tool["name"])]).model_copy(
                    update={"generated_tasks": ["고객 ID 목록"]}
                )
            )
        await toggle(client, tool, False)
        return StructuredResult(parsed=ok("SELECT id FROM customers", [], suggestion=False))

    provider.generate_structured = changed
    response = await ask(client, context, question="서울 고객 수와 고객 ID 목록")
    assert response.status_code == 409
    assert response.json()["error"] == "tool_changed"
    assert "operations" not in response.json()
    assert calls == 2
