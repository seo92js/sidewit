"""Renaming keeps Tool identity and persists as one atomic file update."""

import asyncio
import json
import uuid

import pytest

import sidewit.store as storage
from sidewit.generation.providers.base import StructuredResult
from sidewit.store import Store, StoreError
from tests.test_generate_api import ask
from tests.test_generate_api import fake as fake
from tests.test_tool_activation import toggle
from tests.test_tool_plan import call, plan
from tests.test_tools_api import ARGS, tool_body
from tests.test_tools_api import context as context
from tests.test_tools_api import tool as tool
from tests.tool_urls import tool_path


async def rename(client, tool, name, **changes):
    return await client.put(
        f"/api/v1/tools/{tool['id']}",
        json={
            **{key: tool[key] for key in ("description", "input_schema", "operation")},
            "expected_version": tool["version"],
            "name": name,
            **changes,
        },
    )


@pytest.mark.parametrize("legacy_file", [False, True])
async def test_rename_preserves_identity_restart_and_name_reuse(client, tool, store, legacy_file):
    original = (await toggle(client, tool, False)).json()
    path = store.root / "tools" / f"{tool['id']}.json"
    if legacy_file:
        legacy = path.with_name(f"{tool['name']}.json")
        path.rename(legacy)
        path = legacy
        store.restore()
    result = await rename(client, original, "renamed_revenue")
    assert result.status_code == 200, result.text
    renamed = result.json()
    assert renamed["name"] == "renamed_revenue"
    assert renamed["version"] == original["version"] + 1
    for key in original.keys() - {"name", "version", "updated_at"}:
        assert renamed[key] == original[key]
    assert json.loads(path.read_text())["name"] == renamed["name"]
    assert list(path.parent.glob("*.json")) == [path]
    assert store.tool(tool["name"]) is None
    assert (await client.get(f"/api/v1/tools/{tool['id']}")).json()["name"] == renamed["name"]
    assert (await rename(client, renamed, renamed["name"])).json()["version"] == renamed["version"]

    # Reusing the old name must never overwrite a legacy file bearing that name.
    reused = await client.post("/api/v1/tools", json=tool_body({"id": tool["context_id"]}))
    assert reused.status_code == 201
    assert reused.json()["id"] != tool["id"]
    root = store.root
    store.close()
    reopened = Store(root)
    try:
        assert reopened.tool(renamed["name"]).id == uuid.UUID(tool["id"])
        assert reopened.tool(renamed["name"]).enabled is False
        assert reopened.tool(tool["name"]).id == uuid.UUID(reused.json()["id"])
        reopened.delete_tool(reopened.tool(renamed["name"]))
        assert not path.exists()
        assert reopened.tool(tool["name"]) is not None
    finally:
        reopened.close()


async def test_rename_can_edit_and_invoke_with_one_version_increment(client, tool):
    renamed = await rename(client, tool, "new_revenue", description="Updated description")
    assert renamed.status_code == 200
    current = renamed.json()
    assert current["description"] == "Updated description" and current["version"] == 2
    invoked = await client.post(
        tool_path("new_revenue", "/invoke"),
        json={"arguments": ARGS, "expected_version": 2},
    )
    assert invoked.status_code == 200
    assert invoked.json()["tool"] == "new_revenue"
    assert (await rename(client, current, "another_name", expected_version=1)).status_code == 409


@pytest.mark.parametrize("name", ["", "Upper", "../escape", "a" * 65, "has space"])
async def test_invalid_rename_is_not_persisted(client, tool, name):
    assert (await rename(client, tool, name)).status_code == 422
    assert (await client.get(f"/api/v1/tools/{tool['id']}")).json() == tool


async def test_duplicate_name_and_invalid_sql_leave_both_tools_unchanged(client, tool, context):
    other = (await client.post("/api/v1/tools", json=tool_body(context, name="taken"))).json()
    response = await rename(client, tool, "taken")
    assert response.status_code == 409 and response.json()["error"] == "tool_name_conflict"
    response = await rename(
        client,
        tool,
        "new_name",
        operation={"type": "sql", "statement": "SELECT missing FROM orders"},
    )
    assert response.status_code == 422
    assert (await client.get(f"/api/v1/tools/{tool['id']}")).json() == tool
    assert (await client.get(tool_path("taken"))).json() == other


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("legacy_file", [False, True])
async def test_failed_rename_recovers_one_identity_and_file(
    client, tool, store, monkeypatch, committed, legacy_file
):
    path = store.root / "tools" / f"{tool['id']}.json"
    if legacy_file:
        legacy = path.with_name(f"{tool['name']}.json")
        path.rename(legacy)
        path = legacy
        store.restore()

    def fail(*args):
        raise OSError("simulated storage failure")

    with monkeypatch.context() as patch:
        if committed:
            patch.setattr(storage, "_sync_directory", fail)
        else:
            patch.setattr(storage.os, "replace", fail)
        assert (await rename(client, tool, "new_name")).status_code == 503
    current = (await client.get("/api/v1/tools")).json()["items"]
    assert len(current) == 1
    assert current[0]["id"] == tool["id"]
    assert current[0]["name"] == ("new_name" if committed else tool["name"])
    assert current[0]["version"] == 1 + int(committed)
    assert list(path.parent.glob("*.json")) == [path]
    store.close()
    reopened = Store(store.root)
    try:
        assert len(reopened.tools) == 1
        assert reopened.tool(current[0]["name"]).id == uuid.UUID(tool["id"])
    finally:
        reopened.close()


@pytest.mark.parametrize("duplicate", ["id", "name"])
async def test_duplicate_identity_or_name_prevents_startup(client, tool, store, duplicate):
    path = store.root / "tools" / f"{tool['id']}.json"
    data = json.loads(path.read_text())
    if duplicate == "id":
        data["name"] = "another_name"
        target = path.with_name("another_name.json")
    else:
        data["id"] = str(uuid.uuid4())
        target = path.with_name(f"{data['id']}.json")
    target.write_text(json.dumps(data))
    store.close()
    with pytest.raises(StoreError, match=f"duplicate Tool {duplicate}"):
        Store(store.root)


async def test_concurrent_renames_cannot_overwrite_another_tool(client, tool, context):
    other = (await client.post("/api/v1/tools", json=tool_body(context, name="other"))).json()
    results = await asyncio.gather(rename(client, tool, "shared"), rename(client, other, "shared"))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len((await client.get("/api/v1/tools")).json()["items"]) == 2


async def test_rename_during_selection_rejects_snapshot(client, context, tool, fake):
    provider = fake()

    async def changed(*args):
        assert (await rename(client, tool, "renamed_revenue")).status_code == 200
        return StructuredResult(parsed=plan([call(name=tool["name"])]))

    provider.generate_structured = changed
    response = await ask(client, context, question="서울 매출")
    assert response.status_code == 409
    assert response.json()["error"] == "tool_changed"
    assert "operations" not in response.json()


async def test_old_name_reuse_cannot_redirect_requests_to_another_identity(client, context, tool):
    renamed = (await rename(client, tool, "renamed_revenue")).json()
    replacement = (
        await client.post("/api/v1/tools", json=tool_body(context, description="Another Tool"))
    ).json()
    assert replacement["id"] != tool["id"] and replacement["version"] == tool["version"]
    response = await rename(client, tool, tool["name"], description="Stale editor contents")
    assert response.status_code == 409
    assert response.json()["error"] == "version_conflict"
    address = f"/api/v1/tools/{tool['id']}"
    assert (
        await client.patch(address, json={"expected_version": 1, "enabled": False})
    ).status_code == 409
    assert (
        await client.post(address + "/invoke", json={"expected_version": 1, "arguments": ARGS})
    ).status_code == 409
    assert (
        await client.post(address + "/refresh-contract", json={"expected_version": 1})
    ).status_code == 409
    assert (await client.get(address)).json() == renamed
    assert (await client.delete(address)).status_code == 204
    assert (await client.get(address)).status_code == 404
    assert (await client.get(f"/api/v1/tools/{replacement['id']}")).json() == replacement


@pytest.mark.parametrize("name", ["new", "draft"])
async def test_names_do_not_affect_resource_addresses(client, tool, name):
    response = await rename(client, tool, name)
    assert response.status_code == 200
    assert (await client.get(f"/api/v1/tools/{tool['id']}")).json() == response.json()
    invoked = await client.post(f"/api/v1/tools/{tool['id']}/invoke", json={"arguments": ARGS})
    assert invoked.status_code == 200
    assert invoked.json()["tool_id"] == tool["id"] and invoked.json()["tool"] == name


async def test_name_addressed_requests_are_not_supported(client, tool):
    address = "/api/v1/tools/" + tool["name"]
    for method, suffix, body in (
        ("GET", "", None),
        (
            "PUT",
            "",
            {
                **{k: tool[k] for k in ("description", "input_schema", "operation")},
                "expected_version": 1,
            },
        ),
        ("PATCH", "", {"enabled": False, "expected_version": 1}),
        ("DELETE", "", None),
        ("POST", "/invoke", {"arguments": ARGS}),
        ("POST", "/refresh-contract", {"expected_version": 1}),
    ):
        response = await client.request(method, address + suffix, json=body)
        assert response.status_code == 422
    assert (await client.get(f"/api/v1/tools/{tool['id']}")).json() == tool
