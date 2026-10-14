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


async def test_rhs_cast_valid_literal(client):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {
                "type": "sql",
                "statement": "SELECT status FROM orders WHERE status = CAST('AX' AS varchar(1))",
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"], r.text


@pytest.mark.parametrize("rhs", ["(SELECT :s)", "ANY(SELECT :s)", "(VALUES (:s))"])
async def test_subquery_parameter_domain(client, rhs):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "probe_tool",
            "description": "probe",
            "operation": {
                "type": "sql",
                "statement": "SELECT status FROM orders WHERE status = " + rhs,
            },
            "input_schema": input_schema_for({"s": "string"}),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(tool_path("probe_tool", "/invoke"), json={"arguments": {"s": "OUTSIDE"}})
    assert r.status_code == 422, r.text
