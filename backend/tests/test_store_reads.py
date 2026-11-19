"""Reads share stored snapshots and one parsed definition; changes never touch them."""

import os
import uuid
from dataclasses import asdict, replace

import pytest

import sidewit.store
from sidewit.domain.context import ContextDefinition
from sidewit.store import ContextRecord
from tests.test_tools_api import ARGS, DEFINITION, tool_body
from tests.test_tools_api import context as context

NAMES = ("first_revenue", "second_revenue", "third_revenue")


@pytest.fixture
async def tools(client, context):
    created = []
    for name in NAMES:
        response = await client.post("/api/v1/tools", json=tool_body(context, name=name))
        assert response.status_code == 201, response.text
        created.append(response.json())
    return created


@pytest.fixture
def work(monkeypatch):
    """Counts record copies made by the store and explicit Context definition parses."""
    counts = {"copies": 0, "parses": 0}
    deepcopy = sidewit.store.deepcopy
    parse = ContextDefinition.model_validate.__func__

    def counted_copy(value, *args):
        counts["copies"] += 1
        return deepcopy(value, *args)

    def counted_parse(cls, *args, **kwargs):
        if cls is ContextDefinition:
            counts["parses"] += 1
        return parse(cls, *args, **kwargs)

    monkeypatch.setattr(sidewit.store, "deepcopy", counted_copy)
    monkeypatch.setattr(ContextDefinition, "model_validate", classmethod(counted_parse))
    return counts


async def test_read_and_invoke_paths_neither_copy_records_nor_reparse_definition(
    client, context, tools, work
):
    for path in ("/api/v1/tools", "/api/v1/contexts", f"/api/v1/contexts/{context['id']}"):
        assert (await client.get(path)).status_code == 200
    for tool in tools:
        assert (await client.get(f"/api/v1/tools/{tool['id']}")).status_code == 200
        for _ in range(2):
            invoked = await client.post(
                f"/api/v1/tools/{tool['id']}/invoke", json={"arguments": ARGS}
            )
            assert invoked.status_code == 200, invoked.text
    assert work == {"copies": 0, "parses": 0}


async def test_context_update_parses_the_new_definition_once_for_all_tools(
    client, store, context, tools, work
):
    body = {k: context[k] for k in ("name", "dialect", "timezone", "allow_generated_sql")}
    changed = DEFINITION | {"rules": []}
    response = await client.put(
        f"/api/v1/contexts/{context['id']}",
        json=body | {"definition": changed, "expected_version": context["version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revalidation"]["checked"] == len(tools)
    assert work["parses"] == 1
    snapshot = store.context_snapshot(uuid.UUID(context["id"]))
    assert store.definition(snapshot).to_json() == snapshot.definition


async def test_stale_tool_invoked_from_a_snapshot_is_saved_as_a_new_record(
    client, store, context, tools
):
    stored = store.context_snapshot(uuid.UUID(context["id"]))
    working = store.save_context(replace(stored, version=stored.version + 1))
    shared = store.tool_snapshot(uuid.UUID(tools[0]["id"]))
    before = asdict(shared)
    response = await client.post(f"/api/v1/tools/{tools[0]['id']}/invoke", json={"arguments": ARGS})
    assert response.status_code == 200
    assert response.json()["context_version"] == working.version
    assert asdict(shared) == before
    assert store.tool_snapshot(shared.id).context_version == working.version


async def test_failed_change_leaves_snapshots_held_by_readers_unchanged(
    client, store, context, tools, monkeypatch
):
    tool = tools[0]
    tool_snapshot = store.tool_snapshot(uuid.UUID(tool["id"]))
    context_snapshot = store.context_snapshot(uuid.UUID(context["id"]))
    held = (asdict(tool_snapshot), asdict(context_snapshot))

    def fail(*args):
        raise OSError("fixture failure")

    monkeypatch.setattr(os, "replace", fail)
    body = {k: tool[k] for k in ("description", "input_schema", "operation")}
    for request in (
        client.put(
            f"/api/v1/tools/{tool['id']}",
            json=body | {"expected_version": 1, "name": "renamed", "description": "changed"},
        ),
        client.patch(f"/api/v1/tools/{tool['id']}", json={"expected_version": 1, "enabled": False}),
        client.put(
            f"/api/v1/contexts/{context['id']}",
            json={k: context[k] for k in ("dialect", "timezone", "allow_generated_sql")}
            | {"name": "renamed", "definition": DEFINITION, "expected_version": 1},
        ),
    ):
        assert (await request).status_code == 503
    assert (asdict(tool_snapshot), asdict(context_snapshot)) == held


def test_save_keeps_a_private_copy_and_leaves_the_argument_unchanged(store):
    record = ContextRecord(name="orders", dialect="postgresql", definition={"resources": []})
    before = asdict(record)
    saved = store.save_context(record)
    assert asdict(record) == before
    assert saved is store.context_snapshot(record.id) and saved is not record
    assert saved.updated_at >= record.updated_at
    record.definition["resources"].append({"name": "changed"})
    assert store.context_snapshot(record.id).definition == {"resources": []}


def test_definition_cache_belongs_to_the_stored_snapshot(store):
    saved = store.save_context(
        ContextRecord(name="orders", dialect="postgresql", definition={"resources": []})
    )
    first = store.definition(saved)
    assert store.definition(saved) is first
    working = replace(saved, definition={"resources": [{"name": "orders", "fields": []}]})
    # An unsaved working copy is parsed on its own, never from or into the cache.
    assert [r.name for r in store.definition(working).resources] == ["orders"]
    assert store.definition(saved) is first
    resaved = store.save_context(working)
    assert [r.name for r in store.definition(resaved).resources] == ["orders"]
    # The replaced snapshot keeps describing its own content.
    assert store.definition(saved).resources == []
