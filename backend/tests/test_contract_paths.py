import pytest

from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context
from tests.tool_urls import tool_path

D = {
    "resources": [
        {
            "name": "orders",
            "fields": [{"name": "status", "type": "text", "allowed_values": ["A", "B"]}],
        }
    ]
}


@pytest.mark.parametrize("rhs", ["CAST(:s AS varchar(1))", "CAST((SELECT :s) AS varchar(1))"])
async def test_transformed_invoke_matches_validate(client, rhs):
    ctx = await create_context(client, D)
    sql = "SELECT status FROM orders WHERE status = " + rhs
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {
                "statement": sql,
                "parameter_types": {"s": "string"},
                "parameters": {"s": "ZX"},
            },
        },
    )
    assert not r.json()["valid"], r.text
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "probe",
            "description": "probe",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": input_schema_for({"s": "string"}),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(tool_path("probe", "/invoke"), json={"arguments": {"s": "AX"}})
    assert r.status_code == 200, r.text
    r = await client.post(tool_path("probe", "/invoke"), json={"arguments": {"s": "ZX"}})
    assert r.status_code == 422, r.text


async def test_cte_name_only_visible_in_its_scope(client):
    ctx = await create_context(client, D)
    sql = (
        "SELECT rolname FROM pg_roles WHERE EXISTS "
        "(WITH pg_roles AS (SELECT 1) SELECT 1 FROM pg_roles)"
    )
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "probe",
            "description": "probe",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": input_schema_for({}),
        },
    )
    assert r.status_code == 422, r.text


async def test_large_numeric_cast_not_500(client):
    ctx = await create_context(client, {"resources": []})
    client._transport.raise_app_exceptions = False
    r = await client.post(
        "/api/v1/contexts/" + ctx["id"] + "/import-ddl",
        json={"ddl": "CREATE TABLE t(code numeric CHECK(code IN (1::numeric(40,30))))"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "sql",
    [
        "WITH pg_roles AS (SELECT 'A' AS status) SELECT status FROM pg_roles",
        "WITH q AS (SELECT status FROM orders) SELECT status FROM q",
        "WITH RECURSIVE q(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM q WHERE n<3) SELECT n FROM q",
    ],
)
async def test_legitimate_cte_remains_valid(client, sql):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {"statement": sql},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"], r.text
