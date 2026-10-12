import logging

import pytest

from sidewit.api.generate import get_llm_provider
from sidewit.domain.context import ContextDefinition
from sidewit.generation.dto import LLMGenerationOutput
from sidewit.generation.providers.fake import FakeProvider
from sidewit.main import app
from sidewit.sql import MAX_AST_DEPTH, MAX_NESTING
from sidewit.validation.sql_validator import validate_sql_operation
from tests.test_contexts_api import create, update_body


def output(statement):
    return LLMGenerationOutput(
        status="ok",
        reason=None,
        clarifications=[],
        statement=statement,
        parameters=[],
        tool_suggestion=None,
    )


async def test_ddl_preview_version_prevents_lost_update(client):
    context = await create(client)
    path = f"/api/v1/contexts/{context['id']}"
    request = {"ddl": "CREATE TABLE added (id int)", "expected_version": 1}
    preview = (await client.post(path + "/import-ddl", json=request)).json()
    assert preview["context_version"] == 1
    updated = await client.put(path, json=update_body(context, name="remote"))
    assert updated.status_code == 200
    stale_preview = await client.post(path + "/import-ddl", json=request)
    assert stale_preview.status_code == 409
    stale_save = await client.put(
        path,
        json=update_body(
            context, definition=preview["definition"], expected_version=preview["context_version"]
        ),
    )
    assert stale_save.status_code == 409
    assert (await client.get(path)).json() == updated.json()["context"]
    request["expected_version"] = 2
    fresh = (await client.post(path + "/import-ddl", json=request)).json()
    assert fresh["context_version"] == 2
    saved = await client.put(
        path,
        json=update_body(
            updated.json()["context"],
            definition=fresh["definition"],
            expected_version=fresh["context_version"],
        ),
    )
    assert saved.status_code == 200


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "(" * 50 + "1" + ")" * 50,
        "SELECT " + " + ".join(["1"] * (MAX_AST_DEPTH + 5)),
        "SELECT " + "CASE WHEN TRUE THEN " * 100 + "1" + " END" * 100,
    ],
)
async def test_complex_sql_returns_validation_error_and_generation_can_repair(client, sql):
    context = await create(client)
    response = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {"statement": sql},
        },
    )
    assert response.status_code == 200
    assert not response.json()["valid"]
    assert "limit" in response.json()["errors"][0]["message"]
    # No uncaught error in Tool save either, and no file is saved.
    tool = await client.post(
        "/api/v1/tools",
        json={
            "name": "nested",
            "context_id": context["id"],
            "description": "nested",
            "operation": {"statement": sql},
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    )
    assert tool.status_code == 422
    assert (await client.get("/api/v1/tools")).json()["items"] == []
    provider = FakeProvider([output(sql), output("SELECT 1")])
    app.dependency_overrides[get_llm_provider] = lambda: provider
    generated = await client.post(
        "/api/v1/generate", json={"context_id": context["id"], "question": "count"}
    )
    assert generated.status_code == 200
    assert generated.json()["status"] == "ok"
    assert len(provider.calls) == 2


async def test_nested_ddl_returns_json_error_without_changing_context(client):
    context = await create(client)
    path = f"/api/v1/contexts/{context['id']}"
    ddl = "CREATE TABLE t (id int CHECK (" + "(" * 50 + "id > 0" + ")" * 50 + "))"
    response = await client.post(path + "/import-ddl", json={"ddl": ddl})
    assert response.status_code == 422
    assert response.json()["error"] == "ddl_parse_error"
    assert (await client.get(path)).json() == context


def test_nesting_limit_ignores_literals_and_comments():
    sql = "SELECT '" + "(" * 100 + "' /*" + "(" * 100 + "*/"
    assert validate_sql_operation(sql, "postgresql", ContextDefinition(), {}).valid
    sql = "SELECT " + "(" * MAX_NESTING + "1" + ")" * MAX_NESTING
    assert validate_sql_operation(sql, "postgresql", ContextDefinition(), {}).valid


async def test_ddl_and_generation_sql_never_leak_through_dependency_logs(client, caplog):
    caplog.set_level(logging.WARNING, logger="sqlglot")
    context = await create(client)
    sql = "CREATE POLICY private_policy ON private_table USING (owner = 'REVIEW_PRIVATE_VALUE')"
    response = await client.post(f"/api/v1/contexts/{context['id']}/import-ddl", json={"ddl": sql})
    assert response.status_code == 200
    assert response.json()["warnings"]  # Reviewable details remain in the authenticated response.
    provider = FakeProvider([output(sql), output(sql)])
    app.dependency_overrides[get_llm_provider] = lambda: provider
    generated = await client.post(
        "/api/v1/generate", json={"context_id": context["id"], "question": "count"}
    )
    assert generated.status_code == 422
    assert generated.json()["error"] == "generation_invalid"
    assert any(r.name == "sqlglot" for r in caplog.records)
    for private in ("REVIEW_PRIVATE_VALUE", "private_table", "private_policy", "CREATE POLICY"):
        assert private not in caplog.text


async def test_sequential_ddl_uses_unsaved_base_and_resolves_pending_relations(client):
    context = await create(client)
    path = f"/api/v1/contexts/{context['id']}"
    base = ContextDefinition().model_dump(mode="json")
    first = await client.post(
        path + "/import-ddl",
        json={
            "ddl": "CREATE TABLE orders (id int PRIMARY KEY, user_id int REFERENCES users(id))",
            "expected_version": 1,
            "base_definition": base,
        },
    )
    assert first.status_code == 200
    first = first.json()
    assert len(first["pending_foreign_keys"]) == 1
    draft = first["definition"]
    draft["resources"][0]["description"] = "User-written description, not saved"
    second = await client.post(
        path + "/import-ddl",
        json={
            "ddl": "CREATE TABLE users (id int PRIMARY KEY)",
            "expected_version": 1,
            "base_definition": draft,
            "pending_foreign_keys": first["pending_foreign_keys"],
        },
    )
    assert second.status_code == 200
    second = second.json()
    assert [r["name"] for r in second["definition"]["resources"]] == ["orders", "users"]
    assert (
        second["definition"]["resources"][0]["description"] == draft["resources"][0]["description"]
    )
    assert second["definition"]["relations"] == [{"from": "orders.user_id", "to": "users.id"}]
    assert second["pending_foreign_keys"] == []
    # Preview never writes; one final save persists the combined draft.
    assert (await client.get(path)).json() == context
    saved = await client.put(path, json=update_body(context, definition=second["definition"]))
    assert saved.status_code == 200
    repeated = await client.post(
        path + "/import-ddl",
        json={
            "ddl": "CREATE TABLE users (id int PRIMARY KEY)",
            "expected_version": 2,
            "base_definition": second["definition"],
        },
    )
    assert repeated.json()["definition"] == second["definition"]
    stale = await client.post(
        path + "/import-ddl",
        json={
            "ddl": "CREATE TABLE other (id int)",
            "expected_version": 1,
            "base_definition": second["definition"],
        },
    )
    assert stale.status_code == 409


async def test_draft_preview_requires_version_and_bounds_combined_schema(client):
    context = await create(client)
    path = f"/api/v1/contexts/{context['id']}/import-ddl"
    base = ContextDefinition().model_dump(mode="json")
    response = await client.post(
        path, json={"ddl": "CREATE TABLE t (id int)", "base_definition": base}
    )
    assert response.status_code == 422
    base["resources"] = [{"name": f"t{i}", "fields": []} for i in range(100)]
    response = await client.post(
        path,
        json={
            "ddl": "CREATE TABLE extra (id int)",
            "base_definition": base,
            "expected_version": 1,
        },
    )
    assert response.status_code == 422
