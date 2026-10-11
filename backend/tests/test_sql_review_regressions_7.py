import pytest

from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context

D = {
    "resources": [
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "integer", "primary_key": True, "nullable": False},
                {"name": "status", "type": "text"},
            ],
        }
    ]
}


async def test_catalog_not_context_resource(client):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "catalog_tool",
            "description": "probe",
            "operation": {"type": "sql", "statement": "SELECT rolname FROM pg_roles"},
            "input_schema": input_schema_for({}),
        },
    )
    assert r.status_code == 422, r.text


async def test_primary_key_group_by(client):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {"type": "sql", "statement": "SELECT id,status FROM orders GROUP BY id"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"], r.text


@pytest.mark.parametrize(
    "ddl,expected",
    [
        ("CREATE TABLE t(status text CHECK(status IN ('ABC'::varchar(1))))", ["A"]),
        ("CREATE TABLE t(amount numeric CHECK(amount IN (1.234::numeric(4,2))))", [1.23]),
    ],
)
async def test_cast_modifiers_preserved(client, ddl, expected):
    ctx = await create_context(client, {"resources": []})
    r = await client.post("/api/v1/contexts/" + ctx["id"] + "/import-ddl", json={"ddl": ddl})
    assert r.status_code == 200, r.text
    body = r.json()
    actual = body["definition"]["resources"][0]["fields"][0]["allowed_values"]
    assert actual == expected or (actual == [] and body["warnings"]), r.text


async def test_transformed_column_not_original_domain(client):
    ctx = await create_context(client, {"resources": []})
    r = await client.post(
        "/api/v1/contexts/" + ctx["id"] + "/import-ddl",
        json={"ddl": "CREATE TABLE t(status text CHECK(status::varchar(1) IN ('A','B')))"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["resources"][0]["fields"][0]["allowed_values"] == [], r.text


async def test_parenthesized_join_alias_keeps_allowed_values(client):
    ctx = await create_context(
        client,
        {
            "resources": [
                {
                    "name": "orders",
                    "fields": [
                        {"name": "id", "type": "integer"},
                        {"name": "status", "type": "text", "allowed_values": ["A", "B"]},
                    ],
                },
                {"name": "customers", "fields": [{"name": "id", "type": "integer"}]},
            ]
        },
    )
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "joined_tool",
            "description": "probe",
            "operation": {
                "type": "sql",
                "statement": (
                    "SELECT j.status FROM (orders JOIN customers USING(id)) AS j WHERE j.status=:s"
                ),
            },
            "input_schema": input_schema_for({"s": "string"}),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/tools/joined_tool/invoke", json={"arguments": {"s": "OUTSIDE"}})
    assert r.status_code == 422, r.text
