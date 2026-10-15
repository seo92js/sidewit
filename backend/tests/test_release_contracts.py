"""Release review regressions: verification evidence, concurrency, bounds and retention."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sidewit.application.runtime import get_llm_provider
from sidewit.generation.history import GenerationRecord
from sidewit.main import app
from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context, create_tool
from tests.tool_urls import tool_path

DEFINITION = {"resources": [{"name": "orders", "fields": [{"name": "id", "type": "bigint"}]}]}


async def test_tool_updates_require_the_editor_version(client):
    context = await create_context(client, DEFINITION)
    tool = await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    body = {k: tool[k] for k in ("description", "input_schema", "operation")}
    missing = await client.put(tool_path("review_tool"), json=body)
    assert missing.status_code == 422
    updated = await client.put(
        tool_path("review_tool"),
        json=body
        | {
            "expected_version": 1,
            "operation": {"type": "sql", "statement": "SELECT id FROM orders WHERE id > 10"},
        },
    )
    assert updated.status_code == 200, updated.text
    stale = await client.put(
        tool_path("review_tool"),
        json=body | {"expected_version": 1, "description": "old editor"},
    )
    assert stale.status_code == 409 and stale.json()["error"] == "version_conflict"
    current = (await client.get(tool_path("review_tool"))).json()
    assert current["operation"] == updated.json()["operation"]
    invoked = await client.post(
        tool_path("review_tool", "/invoke"), json={"arguments": {}, "expected_version": 1}
    )
    assert invoked.status_code == 409


async def test_nonblocking_warnings_survive_save_and_invoke(client):
    definition = {
        "resources": [
            {"name": "orders", "fields": [{"name": "id", "type": "bigint"}]},
            {"name": "customers", "fields": [{"name": "id", "type": "bigint"}]},
        ]
    }
    context = await create_context(client, definition)
    sql = "SELECT o.id FROM orders o JOIN customers c ON c.id = o.id"
    tool = await create_tool(client, context, sql, input_schema_for({}))
    assert "undeclared_relation" in {w["code"] for w in tool["validation"]["warnings"]}
    invoked = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
    assert invoked.json()["validation"] == tool["validation"]


async def test_stale_tool_that_no_longer_validates_is_blocked_on_invoke(client, store):
    context = await create_context(client, DEFINITION)
    tool = await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    record = store.tool(tool["name"])
    record.operation = {"type": "sql", "statement": "SELECT missing FROM orders"}
    record.status = "stale"
    record.validation = None
    store.save_tool(record)
    res = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
    assert res.status_code == 409
    stored = (await client.get(tool_path("review_tool"))).json()
    assert stored["status"] == "invalid"
    assert [e["code"] for e in stored["validation"]["errors"]] == ["unknown_field"]


async def test_static_validation_limits_functions_but_does_not_judge_types(client):
    """Function policy is enforced; custom field types still belong to the database."""
    context = await create_context(client, DEFINITION)
    check = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {"statement": "SELECT pg_advisory_lock(1)"},
        },
    )
    assert not check.json()["valid"]
    assert check.json()["errors"][0]["code"] == "function_not_allowed"
    definition = {
        "resources": [{"name": "items", "fields": [{"name": "value", "type": "private_domain"}]}]
    }
    other = (
        await client.post(
            "/api/v1/contexts",
            json={"name": "other", "dialect": "postgresql", "definition": definition},
        )
    ).json()
    check = await client.post(
        "/api/v1/validate",
        json={"context_id": other["id"], "operation": {"statement": "SELECT value FROM items"}},
    )
    assert check.json()["valid"], check.json()


def test_unanalyzed_value_policy_is_rejected():
    from unittest.mock import patch

    from sidewit.domain.context import ContextDefinition
    from sidewit.validation.sql_analysis import SqlAnalysisError
    from sidewit.validation.sql_validator import validate_sql_operation

    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "orders",
                    "fields": [{"name": "status", "type": "text", "allowed_values": ["A"]}],
                }
            ]
        }
    )
    with patch(
        "sidewit.validation.sql_validator.SqlAnalysis",
        side_effect=SqlAnalysisError("unsupported scope"),
    ):
        static = validate_sql_operation(
            "SELECT status FROM orders WHERE status = :s", "postgresql", definition, {"s": "string"}
        )
    assert not static.valid
    assert "value_unverifiable" in [e.code for e in static.errors]


async def test_generation_deadline_cancels_provider(client, monkeypatch):
    import sidewit.application.generate as endpoint

    cancelled = asyncio.Event()

    class SlowProvider:
        name = "slow"
        model = "fixture"

        async def generate_structured(self, *args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

    async def slow_generation(body, store, history, provider):
        await provider.generate_structured()

    monkeypatch.setattr(endpoint, "_generate_operation", slow_generation)
    context = await create_context(client, DEFINITION)
    app.dependency_overrides[get_llm_provider] = lambda: SlowProvider()
    monkeypatch.setattr(endpoint, "GENERATION_TIMEOUT_SECONDS", 0.05)
    res = await client.post(
        "/api/v1/generate", json={"context_id": context["id"], "question": "IDs"}
    )
    assert res.status_code == 504 and res.json()["error"] == "generation_timeout"
    assert cancelled.is_set()
    assert (await client.get("/api/v1/health")).status_code == 200


async def test_request_and_domain_bounds(client):
    async def chunks():
        for _ in range(17):
            yield b"x" * 65536

    huge = await client.post(
        "/api/v1/contexts", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert huge.status_code == 413 and huge.json()["error"] == "request_too_large"
    context = await create_context(client, DEFINITION)
    res = await client.post(
        "/api/v1/generate", json={"context_id": context["id"], "question": "x" * 8001}
    )
    assert res.status_code == 422
    res = await client.post(
        "/api/v1/contexts",
        json={
            "name": "large",
            "dialect": "postgresql",
            "definition": {"resources": [{"name": f"t{i}", "fields": []} for i in range(101)]},
        },
    )
    assert res.status_code == 422


async def test_history_eviction_preserves_saved_tools(client, store, history):
    context = await create_context(client, DEFINITION)
    tool = await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    history.limit = 1
    records = []
    for i in range(2):
        record = GenerationRecord(
            context_id=UUID(context["id"]),
            context_version=1,
            question=f"private {i}",
            reference_time=datetime.now(UTC),
            provider="fake",
            model="fixture",
            prompt_version="test",
        )
        history.add(record)
        records.append(record)
    saved = store.tool(tool["name"])
    saved.source_generation_id = records[0].id
    store.save_tool(saved)
    assert history.get(records[0].id) is None
    assert history.get(records[1].id) is records[1]
    assert (await client.get(f"/api/v1/generations/{records[0].id}")).status_code == 404
    res = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {}})
    assert res.status_code == 200
    assert store.tool(tool["name"]).source_generation_id == records[0].id
