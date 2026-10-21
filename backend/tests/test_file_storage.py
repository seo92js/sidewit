"""File persistence, interrupted updates and restart boundaries of the single-process store."""

import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sidewit.api.state import get_history, get_store
from sidewit.generation.history import GenerationHistory
from sidewit.main import app
from sidewit.store import Store, StoreError
from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context, create_tool
from tests.tool_urls import tool_path

DEFINITION = {"resources": [{"name": "orders", "fields": [{"name": "id", "type": "bigint"}]}]}


async def test_invalid_unicode_record_is_rejected_before_creating_a_temporary_file(client, store):
    from uuid import UUID

    context = await create_context(client, DEFINITION)
    path = store.root / "contexts" / f"{context['id']}.json"
    before = path.read_bytes()
    record = store.context(UUID(context["id"]))
    record.name = "\ud800"
    with pytest.raises(StoreError, match="unsupported JSON values"):
        store.save_context(record)
    assert path.read_bytes() == before
    assert list(path.parent.glob(".*.json.*")) == []
    assert (await client.get(f"/api/v1/contexts/{context['id']}")).json() == context


@pytest.mark.parametrize("directory", ["contexts", "tools"])
async def test_unreadable_directory_prevents_startup(client, store, monkeypatch, directory):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    files = {p: p.read_bytes() for p in store.root.rglob("*.json")}
    target = store.root / directory
    scan = os.scandir

    def denied(path):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return scan(path)

    store.close()
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", denied)
        with pytest.raises(StoreError, match=f"Could not read directory .*{directory}"):
            Store(store.root)
    assert all(p.read_bytes() == data for p, data in files.items())
    reopened = Store(store.root)
    try:
        assert len(reopened.contexts) == 1
        assert len(reopened.tools) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("directory", ["contexts", "tools"])
async def test_unreadable_directory_cannot_publish_partial_recovery(
    client, store, monkeypatch, directory
):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    before = (store.contexts.copy(), store.tools.copy())
    target = store.root / directory
    scan = os.scandir

    def denied(path):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return scan(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", denied)
        with pytest.raises(StoreError, match="Could not read directory"):
            store.restore()
        assert (store.contexts, store.tools) == before
        assert (await client.get("/api/v1/health")).status_code == 503
        assert (await client.get("/api/v1/contexts")).status_code == 503
    store.restore()
    assert (await client.get("/api/v1/health")).status_code == 200
    assert (
        await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
    ).status_code == 200


@pytest.mark.parametrize("directory", [".", "contexts", "tools"])
async def test_health_checks_directory_read_access(client, store, monkeypatch, directory):
    target = store.root / directory
    scan = os.scandir

    def denied(path):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return scan(path)

    monkeypatch.setattr(os, "scandir", denied)
    res = await client.get("/api/v1/health")
    assert res.status_code == 503
    assert res.json()["storage"] == "unavailable"


def update_body(context, **changes):
    return (
        {
            k: context[k]
            for k in ("name", "dialect", "timezone", "allow_generated_sql", "definition")
        }
        | {"expected_version": context["version"]}
        | changes
    )


async def test_restart_keeps_tool_and_exact_values_but_forgets_generations(client, store):
    definition = {
        "resources": [
            {
                "name": "t",
                "fields": [
                    {"name": "id", "type": "bigint", "allowed_values": [9007199254740993]},
                    {"name": "label", "type": "text", "allowed_values": ["", " 京都\n"]},
                ],
            }
        ]
    }
    context = await create_context(client, definition)
    tool = await create_tool(
        client, context, "SELECT id FROM t WHERE id = :id", input_schema_for({"id": "integer"})
    )
    expected = (
        await client.post(
            tool_path("review_tool", "/invoke"), json={"arguments": {"id": 9007199254740993}}
        )
    ).json()
    root = store.root
    store.close()
    reopened = Store(root)
    app.dependency_overrides[get_store] = lambda: reopened
    app.dependency_overrides[get_history] = lambda: GenerationHistory(200)
    try:
        loaded = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
        assert loaded["definition"] == context["definition"]
        assert (await client.get(tool_path("review_tool"))).json() == tool
        actual = await client.post(
            tool_path("review_tool", "/invoke"), json={"arguments": {"id": 9007199254740993}}
        )
        assert actual.json() == expected
        assert (await client.get("/api/v1/generations")).json() == {"items": []}
    finally:
        reopened.close()


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EACCES])
async def test_failed_replace_keeps_persisted_context_and_memory(
    client, store, monkeypatch, caplog, error_number
):
    context = await create_context(client, DEFINITION)
    path = store.root / "contexts" / f"{context['id']}.json"
    before = path.read_bytes()

    def fail(*args):
        raise OSError(error_number, "private-context-value sk-fixture-secret", "/private/data")

    monkeypatch.setattr(os, "replace", fail)
    result = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, name="changed")
    )
    assert result.status_code == 503
    assert path.read_bytes() == before
    assert (await client.get(f"/api/v1/contexts/{context['id']}")).json() == context
    assert list(path.parent.glob(".*.json.*")) == []
    failures = [
        r.event_fields
        for r in caplog.records
        if getattr(r, "event_name", None) == "storage_failure"
    ]
    assert any(
        f["operation"] == "write" and f["errno"] == error_number and f["recovery"] == "succeeded"
        for f in failures
    )
    for private in ("private-context-value", "sk-fixture-secret", "/private/data"):
        assert private not in caplog.text
        assert private not in result.text


async def test_partial_context_update_cannot_leave_current_wrong_tool(client, store, monkeypatch):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    assert (
        await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
    ).status_code == 200
    replace = os.replace

    def fail_tool(src, target):
        if Path(target).parent.name == "tools":
            raise OSError("fixture failure")
        return replace(src, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_tool)
        changed = {
            "resources": [{"name": "orders", "fields": [{"name": "other", "type": "bigint"}]}]
        }
        result = await client.put(
            f"/api/v1/contexts/{context['id']}", json=update_body(context, definition=changed)
        )
        assert result.status_code == 503
    current = (await client.get(f"/api/v1/contexts/{context['id']}")).json()
    assert current["version"] == 2
    store.close()
    reopened = Store(store.root)
    app.dependency_overrides[get_store] = lambda: reopened
    try:
        invoke = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
        assert invoke.status_code == 409
        assert "unknown_field" in invoke.text
        restored = await client.put(
            f"/api/v1/contexts/{context['id']}", json=update_body(current, definition=DEFINITION)
        )
        assert restored.status_code == 200
        assert (
            await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
        ).status_code == 200
    finally:
        reopened.close()


async def test_failed_recovery_closes_api_until_storage_is_repaired(
    client, store, monkeypatch, caplog
):
    context = await create_context(client, DEFINITION)
    path = store.root / "contexts" / f"{context['id']}.json"
    path.write_text("broken json")

    def fail(*args):
        raise OSError("fixture failure")

    monkeypatch.setattr(os, "replace", fail)
    result = await client.put(
        f"/api/v1/contexts/{context['id']}", json=update_body(context, name="change")
    )
    assert result.status_code == 503
    assert (await client.get("/api/v1/health")).status_code == 503
    assert (await client.get("/api/v1/contexts")).status_code == 503
    failures = [
        r.event_fields
        for r in caplog.records
        if getattr(r, "event_name", None) == "storage_failure"
    ]
    assert any(f["recovery"] == "failed" for f in failures)
    assert any(
        f["operation"] == "validate"
        and f["error_type"] == "StoreError"
        and f["errno"] is None
        and f["recovery"] == "unavailable"
        for f in failures
    )
    assert "broken json" not in caplog.text


async def test_concurrent_context_edits_have_one_winner(client):
    context = await create_context(client, DEFINITION)
    responses = await asyncio.gather(
        *(
            client.put(f"/api/v1/contexts/{context['id']}", json=update_body(context, name=name))
            for name in ("first", "second")
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]


async def test_concurrent_tool_edits_have_one_winner(client):
    context = await create_context(client, DEFINITION)
    tool = await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    body = {k: tool[k] for k in ("description", "operation", "input_schema")}
    responses = await asyncio.gather(
        *(
            client.put(
                tool_path("review_tool"),
                json=body | {"expected_version": 1, "description": description},
            )
            for description in ("first", "second")
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_second_process_cannot_open_the_same_directory(store):
    code = (
        "import sys; from pathlib import Path; from sidewit.store import Store; "
        "Store(Path(sys.argv[1]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.root)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "already owned" in result.stderr
    store.close()
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.root)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"timezone": "not/a/timezone"},
        {"name": " "},
        {"version": 0},
        {"allow_generated_sql": False},
    ],
)
async def test_invalid_context_file_prevents_startup(client, store, change):
    context = await create_context(client, DEFINITION)
    path = store.root / "contexts" / f"{context['id']}.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | change))
    store.close()
    with pytest.raises(StoreError, match="Invalid data files"):
        Store(store.root)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "made_up"},
        {"operation": {}},
        {"input_schema": {}},
        {"context_id": "00000000-0000-0000-0000-000000000000"},
        {"metric_binding": {"unknown": 42}},
    ],
)
async def test_malformed_tool_file_prevents_startup(client, store, change):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    path = store.root / "tools" / f"{store.tool('review_tool').id}.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | change))
    store.close()
    with pytest.raises(StoreError, match="Invalid data files"):
        Store(store.root)


async def test_externally_changed_tool_is_revalidated_on_restart(client, store):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    path = store.root / "tools" / f"{store.tool('review_tool').id}.json"
    data = json.loads(path.read_text())
    data["operation"]["statement"] = "SELECT missing FROM orders"
    path.write_text(json.dumps(data))
    store.close()
    reopened = Store(store.root)
    app.dependency_overrides[get_store] = lambda: reopened
    try:
        result = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
        assert result.status_code == 409
    finally:
        reopened.close()


async def test_restore_discards_cached_verdict_even_when_file_versions_are_unchanged(client, store):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    url = tool_path("review_tool", "/invoke")
    assert (await client.post(url, json={"arguments": {}})).status_code == 200
    path = store.root / "tools" / f"{store.tool('review_tool').id}.json"
    data = json.loads(path.read_text())
    data["operation"]["statement"] = "SELECT missing FROM orders"
    path.write_text(json.dumps(data))
    store.restore()
    result = await client.post(url, json={"arguments": {}})
    assert result.status_code == 409
    assert "unknown_field" in result.text
    tool = (await client.get(tool_path("review_tool"))).json()
    assert tool["status"] == "invalid"
    assert tool["validation_errors"] == tool["validation"]["errors"]


async def test_stores_do_not_share_verdicts_for_identical_ids_and_versions(client, store, tmp_path):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    url = tool_path("review_tool", "/invoke")
    assert (await client.post(url, json={"arguments": {}})).status_code == 200
    other_root = tmp_path / "other"
    shutil.copytree(store.root, other_root)
    path = other_root / "tools" / f"{store.tool('review_tool').id}.json"
    data = json.loads(path.read_text())
    data["operation"]["statement"] = "SELECT missing FROM orders"
    path.write_text(json.dumps(data))
    other = Store(other_root)
    try:
        app.dependency_overrides[get_store] = lambda: other
        assert (await client.post(url, json={"arguments": {}})).status_code == 409
        app.dependency_overrides[get_store] = lambda: store
        assert (await client.post(url, json={"arguments": {}})).status_code == 200
    finally:
        other.close()
        app.dependency_overrides[get_store] = lambda: store


async def test_saving_context_invalidates_verdicts_even_without_version_increment(client, store):
    context = await create_context(client, DEFINITION)
    await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    url = tool_path("review_tool", "/invoke")
    assert (await client.post(url, json={"arguments": {}})).status_code == 200
    record = store.context_named(context["name"])
    record.definition["resources"][0]["fields"] = [{"name": "other", "type": "bigint"}]
    store.save_context(record)
    res = await client.post(url, json={"arguments": {}})
    assert res.status_code == 409
    assert "unknown_field" in res.text
